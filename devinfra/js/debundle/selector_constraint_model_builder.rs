//! Lowering from selector IR plus facts into a compact finite-domain problem.

use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::error::Error;
use std::fmt;

use analysis::{OwnerId, StatementOrdinal};
use chunk_facts::NodeId;
use regex::Regex;
use selector_constraint_backend::{
    AllDifferentReason, BackendValueId, BinaryConstraintKind, CompiledSelectorProblem,
    CompiledSelectorProblemBuilder, CompiledSelectorProblemError, ConstraintValue,
    ConstraintVariableId, TargetBindingProjection,
};
use selector_ir::{
    ClaimKind, NodeTerm, OrdinalTerm, OwnerTerm, SelectorAtom, SelectorFact, SelectorFactStore,
    SelectorProgram, SelectorProgramError, SelectorVariableId, StringTerm, VariableDomain,
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
}

pub fn compile_selector_problem_with_summary(
    program: &SelectorProgram,
    facts: &SelectorFactStore,
) -> Result<CompiledSelectorProblemWithSummary, CompiledSelectorProblemBuildError> {
    program
        .validate()
        .map_err(CompiledSelectorProblemBuildError::InvalidProgram)?;

    let domains = FactDomains::from_program_and_facts(program, facts);
    let summary = domains.summary();
    let target_binding_projections = TargetBindingProjections::from_program(program)?;

    let mut model = CompiledSelectorProblemBuilder::default();
    for domain in [
        VariableDomain::Owner,
        VariableDomain::AstNode,
        VariableDomain::String,
        VariableDomain::StatementOrdinal,
    ] {
        model.add_full_domain_values(domain, domains.values_for(domain))?;
    }
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

    for atom in &program.atoms {
        lower_atom_constraint(atom, &domains, &variables, &mut model)?;
    }

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

    let problem = model
        .finish()
        .map_err(CompiledSelectorProblemBuildError::from)?;
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
) -> Result<(), CompiledSelectorProblemBuildError> {
    match atom {
        SelectorAtom::OwnerKind {
            owner,
            statement_kind,
        } => add_owner_string_allowed_tuples(
            model,
            variables,
            owner,
            statement_kind,
            &domains.owner_kinds,
        ),
        SelectorAtom::OwnerStatementOrdinal { owner, ordinal } => add_owner_ordinal_allowed_tuples(
            model,
            variables,
            owner,
            ordinal,
            &domains.owner_statement_ordinals,
        ),
        SelectorAtom::OwnerDeclaresBinding { owner, binding } => add_owner_string_allowed_tuples(
            model,
            variables,
            owner,
            binding,
            &domains.declared_bindings,
        ),
        SelectorAtom::OwnerExportName { owner, export_name } => add_owner_string_allowed_tuples(
            model,
            variables,
            owner,
            export_name,
            &domains.export_names,
        ),
        SelectorAtom::OwnerReferencesBinding {
            owner,
            binding,
            edge_kind,
        } => {
            let facts = match optional_string_term_const(edge_kind)? {
                Some(edge_kind) => domains
                    .owner_references_binding
                    .iter()
                    .filter_map(|(fact_owner, fact_binding, fact_edge_kind)| {
                        (fact_edge_kind == &edge_kind)
                            .then_some((*fact_owner, fact_binding.clone()))
                    })
                    .collect::<BTreeSet<_>>(),
                None => domains
                    .owner_references_binding
                    .iter()
                    .map(|(fact_owner, fact_binding, _edge_kind)| {
                        (*fact_owner, fact_binding.clone())
                    })
                    .collect::<BTreeSet<_>>(),
            };
            add_owner_string_allowed_tuples(model, variables, owner, binding, &facts)
        }
        SelectorAtom::OwnerReferencesOwner { owner, referenced } => add_owner_owner_allowed_tuples(
            model,
            variables,
            owner,
            referenced,
            &domains.references_owner,
        ),
        SelectorAtom::OwnerAliasesOwner { owner, aliased } => {
            add_owner_owner_allowed_tuples(model, variables, owner, aliased, &domains.aliases_owner)
        }
        SelectorAtom::OwnerTopLevelRoot { owner, root } => add_owner_node_allowed_tuples(
            model,
            variables,
            owner,
            root,
            &domains.owner_top_level_roots,
        ),
        SelectorAtom::AstKind { node, node_kind } => add_node_string_allowed_tuples(
            model,
            variables,
            node,
            &StringTerm::Const {
                value: node_kind.as_tag().to_string(),
            },
            &domains.ast_kinds,
        ),
        SelectorAtom::AstChild {
            parent,
            index,
            child,
        } => add_ast_child_allowed_tuples(
            model,
            variables,
            parent,
            *index,
            child,
            &domains.ast_children_by_parent,
        ),
        SelectorAtom::AstSuperClass {
            class_node,
            super_class,
        } => add_node_node_allowed_tuples(
            model,
            variables,
            class_node,
            super_class,
            &domains.ast_super_classes,
        ),
        SelectorAtom::AstChildCount { node, count } => {
            let facts = domains
                .ast_child_counts
                .iter()
                .filter_map(|(fact_node, fact_count)| {
                    (*fact_count == *count).then_some((*fact_node, fact_count.to_string()))
                })
                .collect::<BTreeSet<_>>();
            add_node_string_allowed_tuples(
                model,
                variables,
                node,
                &StringTerm::Const {
                    value: count.to_string(),
                },
                &facts,
            )
        }
        SelectorAtom::AstStringLiteral { node, value } => add_node_string_allowed_tuples(
            model,
            variables,
            node,
            value,
            &domains.ast_string_literals,
        ),
        SelectorAtom::AstStringLiteralMatchingRegex { node, pattern } => {
            add_ast_string_literal_matching_regex_allowed_tuples(
                model,
                variables,
                node,
                pattern,
                &domains.ast_string_literals,
            )
        }
        SelectorAtom::AstNumberLiteral { node, value } => add_node_string_allowed_tuples(
            model,
            variables,
            node,
            value,
            &domains.ast_number_literals,
        ),
        SelectorAtom::AstBoolLiteral { node, value } => {
            let facts = domains
                .ast_bool_literals
                .iter()
                .filter_map(|(fact_node, fact_value)| {
                    (*fact_value == *value).then_some((*fact_node, fact_value.to_string()))
                })
                .collect::<BTreeSet<_>>();
            add_node_string_allowed_tuples(
                model,
                variables,
                node,
                &StringTerm::Const {
                    value: value.to_string(),
                },
                &facts,
            )
        }
        SelectorAtom::AstIdentifierName { node, value } => add_node_string_allowed_tuples(
            model,
            variables,
            node,
            value,
            &domains.ast_identifier_names,
        ),
        SelectorAtom::AstPropertyName { node, value } => add_node_string_allowed_tuples(
            model,
            variables,
            node,
            value,
            &domains.ast_property_names,
        ),
        SelectorAtom::AstBareProperty {
            node,
            key,
            identifier,
            is_binding,
        } => add_ast_bare_property_allowed_tuples(
            model,
            variables,
            node,
            key,
            identifier,
            *is_binding,
            &domains.ast_bare_properties,
        ),
        SelectorAtom::AstOperator { node, value } => {
            add_node_string_allowed_tuples(model, variables, node, value, &domains.ast_operators)
        }
        SelectorAtom::AstRegexLiteral {
            node,
            pattern,
            flags,
        } => add_ast_regex_literal_allowed_tuples(
            model,
            variables,
            node,
            pattern,
            flags,
            &domains.ast_regex_literals,
        ),
        SelectorAtom::AstTopLevel { node, ordinal } => add_node_ordinal_allowed_tuples(
            model,
            variables,
            node,
            ordinal,
            &domains.ast_top_level_positions,
        ),
        SelectorAtom::OrdinalOffset {
            base,
            ordinal,
            offset,
        } => add_ordinal_offset_constraint(model, variables, base, ordinal, *offset),
        SelectorAtom::ReadsMember {
            owner,
            object: None,
            member,
        } => {
            add_owner_string_allowed_tuples(model, variables, owner, member, &domains.member_reads)
        }
        SelectorAtom::ReadsMember {
            owner,
            object: Some(object),
            member,
        } => {
            let object = required_string_term_const(object, "reads_member.object")?;
            let facts = domains
                .member_reads_from_binding
                .iter()
                .filter_map(|(fact_owner, fact_object, fact_member)| {
                    (fact_object == &object).then_some((*fact_owner, fact_member.clone()))
                })
                .collect::<BTreeSet<_>>();
            add_owner_string_allowed_tuples(model, variables, owner, member, &facts)
        }
        SelectorAtom::ReadsMemberOfOwner {
            owner,
            object,
            member,
        } => {
            let member = required_string_term_const(member, "reads_member_of_owner.member")?;
            let facts = domains
                .reads_member_of_owner
                .iter()
                .filter_map(|(fact_owner, fact_object, fact_member)| {
                    (fact_member == &member).then_some((*fact_owner, *fact_object))
                })
                .collect::<BTreeSet<_>>();
            add_owner_owner_allowed_tuples(model, variables, owner, object, &facts)
        }
        SelectorAtom::ConsumesModuleMember {
            owner,
            module,
            member,
        } => {
            let module = required_string_term_const(module, "consumes_module_member.module")?;
            let member = required_string_term_const(member, "consumes_module_member.member")?;
            let facts = domains
                .module_member_uses
                .iter()
                .filter_map(|(fact_owner, fact_module, fact_member)| {
                    (fact_module == &module && fact_member == &member).then_some(*fact_owner)
                })
                .collect::<BTreeSet<_>>();
            add_owner_allowed_tuples(model, variables, owner, &facts)
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
                .call_arguments
                .iter()
                .filter_map(|(fact_owner, fact_callee_member, fact_arg_index)| {
                    (fact_callee_member == &callee_member
                        && optional_u32_matches(*arg_index, *fact_arg_index))
                    .then_some(*fact_owner)
                })
                .collect::<BTreeSet<_>>();
            add_owner_allowed_tuples(model, variables, owner, &facts)
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
            let facts = domains
                .call_arguments_from_binding
                .iter()
                .filter_map(
                    |(fact_owner, fact_callee_object, fact_callee_member, fact_arg_index)| {
                        (fact_callee_object == &callee_object
                            && fact_callee_member == &callee_member
                            && optional_u32_matches(*arg_index, *fact_arg_index))
                        .then_some(*fact_owner)
                    },
                )
                .collect::<BTreeSet<_>>();
            add_owner_allowed_tuples(model, variables, owner, &facts)
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
                .call_arguments_from_owner
                .iter()
                .filter_map(
                    |(fact_owner, fact_callee_object, fact_callee_member, fact_arg_index)| {
                        (fact_callee_member == &callee_member
                            && optional_u32_matches(*arg_index, *fact_arg_index))
                        .then_some((*fact_owner, *fact_callee_object))
                    },
                )
                .collect::<BTreeSet<_>>();
            add_owner_owner_allowed_tuples(model, variables, owner, callee_object, &facts)
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
                .makes_decorate_call_for_binding
                .iter()
                .filter_map(|(fact_owner, fact_class_anchor, fact_member)| {
                    (fact_class_anchor == &class_anchor
                        && optional_string_matches(&member, fact_member))
                    .then_some(*fact_owner)
                })
                .collect::<BTreeSet<_>>();
            add_owner_allowed_tuples(model, variables, owner, &facts)
        }
        SelectorAtom::MakesDecorateCallForOwner {
            owner,
            class_anchor,
            member,
        } => {
            let member = optional_string_term_const(member)?;
            let facts = domains
                .makes_decorate_call_for_owner
                .iter()
                .filter_map(|(fact_owner, fact_class_anchor, fact_member)| {
                    optional_string_matches(&member, fact_member)
                        .then_some((*fact_owner, *fact_class_anchor))
                })
                .collect::<BTreeSet<_>>();
            add_owner_owner_allowed_tuples(model, variables, owner, class_anchor, &facts)
        }
        SelectorAtom::IntrinsicAlias {
            owner,
            property,
            referenced_by,
        } => {
            let property = required_string_term_const(property, "intrinsic_alias.property")?;
            let facts = domains
                .intrinsic_alias_referenced_by
                .iter()
                .filter_map(|(fact_owner, fact_property, fact_referenced_by)| {
                    (fact_property == &property).then_some((*fact_owner, *fact_referenced_by))
                })
                .collect::<BTreeSet<_>>();
            add_owner_owner_allowed_tuples(model, variables, owner, referenced_by, &facts)
        }
        SelectorAtom::Equal { left, right } => model
            .add_binary_constraint(
                model_variable(variables, *left)?,
                model_variable(variables, *right)?,
                BinaryConstraintKind::Equal,
            )
            .map_err(Into::into),
        SelectorAtom::NotEqual { left, right } => model
            .add_binary_constraint(
                model_variable(variables, *left)?,
                model_variable(variables, *right)?,
                BinaryConstraintKind::NotEqual,
            )
            .map_err(Into::into),
        SelectorAtom::OrdinalBefore { before, after } => {
            add_ordinal_before_constraint(model, variables, before, after)
        }
        SelectorAtom::AstChildListPattern {
            parent,
            start_index,
            segments,
            anchored_left,
            anchored_right,
        } => add_ast_child_list_pattern_allowed_tuples(
            model,
            variables,
            ChildListPatternTerms {
                parent,
                start_index: *start_index,
                segments,
                anchored_left: *anchored_left,
                anchored_right: *anchored_right,
            },
            domains,
        ),
    }
}

fn add_owner_string_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    owner: &OwnerTerm,
    string: &StringTerm,
    facts: &BTreeSet<(OwnerId, String)>,
) -> Result<(), CompiledSelectorProblemBuildError> {
    let mut constraint_variables = Vec::new();
    if let OwnerTerm::Var { id } = owner {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if let StringTerm::Var { id } = string {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if constraint_variables.is_empty() {
        return facts
            .iter()
            .any(|(fact_owner, fact_string)| {
                owner_term_matches(owner, *fact_owner) && string_term_matches(string, fact_string)
            })
            .then_some(())
            .ok_or_else(
                || CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied {
                    atom: format!("owner/string fact {owner:?} {string:?}"),
                },
            );
    }

    let tuples = facts
        .iter()
        .filter(|(fact_owner, fact_string)| {
            owner_term_matches(owner, *fact_owner) && string_term_matches(string, fact_string)
        })
        .map(|(fact_owner, fact_string)| {
            let mut tuple = Vec::with_capacity(constraint_variables.len());
            if matches!(owner, OwnerTerm::Var { .. }) {
                tuple.push(model.intern_owner(*fact_owner)?);
            }
            if matches!(string, StringTerm::Var { .. }) {
                tuple.push(model.intern_string(fact_string)?);
            }
            Ok(tuple)
        })
        .collect::<Result<Vec<_>, CompiledSelectorProblemError>>()?;

    add_encoded_allowed_tuple_set(model, constraint_variables, tuples)
}

fn add_owner_ordinal_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    owner: &OwnerTerm,
    ordinal: &OrdinalTerm,
    facts: &BTreeSet<(OwnerId, StatementOrdinal)>,
) -> Result<(), CompiledSelectorProblemBuildError> {
    let mut constraint_variables = Vec::new();
    if let OwnerTerm::Var { id } = owner {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if let OrdinalTerm::Var { id } = ordinal {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if constraint_variables.is_empty() {
        return facts
            .iter()
            .any(|(fact_owner, fact_ordinal)| {
                owner_term_matches(owner, *fact_owner)
                    && ordinal_term_matches(ordinal, *fact_ordinal)
            })
            .then_some(())
            .ok_or_else(
                || CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied {
                    atom: format!("owner/ordinal fact {owner:?} {ordinal:?}"),
                },
            );
    }

    let tuples = facts
        .iter()
        .filter(|(fact_owner, fact_ordinal)| {
            owner_term_matches(owner, *fact_owner) && ordinal_term_matches(ordinal, *fact_ordinal)
        })
        .map(|(fact_owner, fact_ordinal)| {
            let mut tuple = Vec::with_capacity(constraint_variables.len());
            if matches!(owner, OwnerTerm::Var { .. }) {
                tuple.push(model.intern_owner(*fact_owner)?);
            }
            if matches!(ordinal, OrdinalTerm::Var { .. }) {
                tuple.push(model.intern_statement_ordinal(*fact_ordinal)?);
            }
            Ok(tuple)
        })
        .collect::<Result<Vec<_>, CompiledSelectorProblemError>>()?;

    add_encoded_allowed_tuple_set(model, constraint_variables, tuples)
}

fn add_owner_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    owner: &OwnerTerm,
    facts: &BTreeSet<OwnerId>,
) -> Result<(), CompiledSelectorProblemBuildError> {
    let mut constraint_variables = Vec::new();
    if let OwnerTerm::Var { id } = owner {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if constraint_variables.is_empty() {
        return facts
            .iter()
            .any(|fact_owner| owner_term_matches(owner, *fact_owner))
            .then_some(())
            .ok_or_else(
                || CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied {
                    atom: format!("owner fact {owner:?}"),
                },
            );
    }

    let tuples = facts
        .iter()
        .filter(|fact_owner| owner_term_matches(owner, **fact_owner))
        .map(|fact_owner| model.intern_owner(*fact_owner).map(|value| vec![value]))
        .collect::<Result<Vec<_>, _>>()?;

    add_encoded_allowed_tuple_set(model, constraint_variables, tuples)
}

fn add_owner_owner_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    left: &OwnerTerm,
    right: &OwnerTerm,
    facts: &BTreeSet<(OwnerId, OwnerId)>,
) -> Result<(), CompiledSelectorProblemBuildError> {
    let mut constraint_variables = Vec::new();
    if let OwnerTerm::Var { id } = left {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if let OwnerTerm::Var { id } = right {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if constraint_variables.is_empty() {
        return facts
            .iter()
            .any(|(fact_left, fact_right)| {
                owner_term_matches(left, *fact_left) && owner_term_matches(right, *fact_right)
            })
            .then_some(())
            .ok_or_else(
                || CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied {
                    atom: format!("owner/owner fact {left:?} {right:?}"),
                },
            );
    }

    let tuples = facts
        .iter()
        .filter(|(fact_left, fact_right)| {
            owner_term_matches(left, *fact_left) && owner_term_matches(right, *fact_right)
        })
        .map(|(fact_left, fact_right)| {
            let mut tuple = Vec::with_capacity(constraint_variables.len());
            if matches!(left, OwnerTerm::Var { .. }) {
                tuple.push(model.intern_owner(*fact_left)?);
            }
            if matches!(right, OwnerTerm::Var { .. }) {
                tuple.push(model.intern_owner(*fact_right)?);
            }
            Ok(tuple)
        })
        .collect::<Result<Vec<_>, CompiledSelectorProblemError>>()?;

    add_encoded_allowed_tuple_set(model, constraint_variables, tuples)
}

fn add_owner_node_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    owner: &OwnerTerm,
    node: &NodeTerm,
    facts: &BTreeSet<(OwnerId, NodeId)>,
) -> Result<(), CompiledSelectorProblemBuildError> {
    let mut constraint_variables = Vec::new();
    if let OwnerTerm::Var { id } = owner {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if let NodeTerm::Var { id } = node {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if constraint_variables.is_empty() {
        return facts
            .iter()
            .any(|(fact_owner, fact_node)| {
                owner_term_matches(owner, *fact_owner) && node_term_matches(node, *fact_node)
            })
            .then_some(())
            .ok_or_else(
                || CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied {
                    atom: format!("owner/node fact {owner:?} {node:?}"),
                },
            );
    }

    let tuples = facts
        .iter()
        .filter(|(fact_owner, fact_node)| {
            owner_term_matches(owner, *fact_owner) && node_term_matches(node, *fact_node)
        })
        .map(|(fact_owner, fact_node)| {
            let mut tuple = Vec::with_capacity(constraint_variables.len());
            if matches!(owner, OwnerTerm::Var { .. }) {
                tuple.push(model.intern_owner(*fact_owner)?);
            }
            if matches!(node, NodeTerm::Var { .. }) {
                tuple.push(model.intern_ast_node(*fact_node)?);
            }
            Ok(tuple)
        })
        .collect::<Result<Vec<_>, CompiledSelectorProblemError>>()?;

    add_encoded_allowed_tuple_set(model, constraint_variables, tuples)
}

fn add_node_node_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    left: &NodeTerm,
    right: &NodeTerm,
    facts: &BTreeSet<(NodeId, NodeId)>,
) -> Result<(), CompiledSelectorProblemBuildError> {
    let mut constraint_variables = Vec::new();
    if let NodeTerm::Var { id } = left {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if let NodeTerm::Var { id } = right {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if constraint_variables.is_empty() {
        return facts
            .iter()
            .any(|(fact_left, fact_right)| {
                node_term_matches(left, *fact_left) && node_term_matches(right, *fact_right)
            })
            .then_some(())
            .ok_or_else(
                || CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied {
                    atom: format!("node/node fact {left:?} {right:?}"),
                },
            );
    }

    let tuples = facts
        .iter()
        .filter(|(fact_left, fact_right)| {
            node_term_matches(left, *fact_left) && node_term_matches(right, *fact_right)
        })
        .map(|(fact_left, fact_right)| {
            let mut tuple = Vec::with_capacity(constraint_variables.len());
            if matches!(left, NodeTerm::Var { .. }) {
                tuple.push(model.intern_ast_node(*fact_left)?);
            }
            if matches!(right, NodeTerm::Var { .. }) {
                tuple.push(model.intern_ast_node(*fact_right)?);
            }
            Ok(tuple)
        })
        .collect::<Result<Vec<_>, CompiledSelectorProblemError>>()?;

    add_encoded_allowed_tuple_set(model, constraint_variables, tuples)
}

fn add_ast_child_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    parent: &NodeTerm,
    child_index: u32,
    child: &NodeTerm,
    ast_children_by_parent: &BTreeMap<NodeId, Vec<(u32, NodeId)>>,
) -> Result<(), CompiledSelectorProblemBuildError> {
    let mut constraint_variables = Vec::new();
    if let NodeTerm::Var { id } = parent {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if let NodeTerm::Var { id } = child {
        constraint_variables.push(model_variable(variables, *id)?);
    }

    let mut tuples = Vec::new();
    let mut constant_only_match = false;
    for (fact_parent, children) in ast_children_by_parent {
        if !node_term_matches(parent, *fact_parent) {
            continue;
        }
        for (fact_index, fact_child) in children {
            if *fact_index != child_index || !node_term_matches(child, *fact_child) {
                continue;
            }
            if constraint_variables.is_empty() {
                constant_only_match = true;
                continue;
            }
            let mut tuple = Vec::with_capacity(constraint_variables.len());
            if matches!(parent, NodeTerm::Var { .. }) {
                tuple.push(model.intern_ast_node(*fact_parent)?);
            }
            if matches!(child, NodeTerm::Var { .. }) {
                tuple.push(model.intern_ast_node(*fact_child)?);
            }
            tuples.push(tuple);
        }
    }

    if constraint_variables.is_empty() {
        return constant_only_match.then_some(()).ok_or_else(|| {
            CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied {
                atom: format!("ast_child fact {parent:?} {child_index} {child:?}"),
            }
        });
    }

    add_encoded_allowed_tuple_set(model, constraint_variables, tuples)
}

fn add_node_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    node: &NodeTerm,
    facts: &BTreeSet<NodeId>,
) -> Result<(), CompiledSelectorProblemBuildError> {
    let mut constraint_variables = Vec::new();
    if let NodeTerm::Var { id } = node {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if constraint_variables.is_empty() {
        return facts
            .iter()
            .any(|fact_node| node_term_matches(node, *fact_node))
            .then_some(())
            .ok_or_else(
                || CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied {
                    atom: format!("node fact {node:?}"),
                },
            );
    }

    let tuples = facts
        .iter()
        .filter(|fact_node| node_term_matches(node, **fact_node))
        .map(|fact_node| model.intern_ast_node(*fact_node).map(|value| vec![value]))
        .collect::<Result<Vec<_>, _>>()?;

    add_encoded_allowed_tuple_set(model, constraint_variables, tuples)
}

fn add_node_string_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    node: &NodeTerm,
    string: &StringTerm,
    facts: &BTreeSet<(NodeId, String)>,
) -> Result<(), CompiledSelectorProblemBuildError> {
    let mut constraint_variables = Vec::new();
    if let NodeTerm::Var { id } = node {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if let StringTerm::Var { id } = string {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if constraint_variables.is_empty() {
        return facts
            .iter()
            .any(|(fact_node, fact_string)| {
                node_term_matches(node, *fact_node) && string_term_matches(string, fact_string)
            })
            .then_some(())
            .ok_or_else(
                || CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied {
                    atom: format!("node/string fact {node:?} {string:?}"),
                },
            );
    }

    let tuples = facts
        .iter()
        .filter(|(fact_node, fact_string)| {
            node_term_matches(node, *fact_node) && string_term_matches(string, fact_string)
        })
        .map(|(fact_node, fact_string)| {
            let mut tuple = Vec::with_capacity(constraint_variables.len());
            if matches!(node, NodeTerm::Var { .. }) {
                tuple.push(model.intern_ast_node(*fact_node)?);
            }
            if matches!(string, StringTerm::Var { .. }) {
                tuple.push(model.intern_string(fact_string)?);
            }
            Ok(tuple)
        })
        .collect::<Result<Vec<_>, CompiledSelectorProblemError>>()?;

    add_encoded_allowed_tuple_set(model, constraint_variables, tuples)
}

fn add_ast_string_literal_matching_regex_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    node: &NodeTerm,
    pattern: &StringTerm,
    facts: &BTreeSet<(NodeId, String)>,
) -> Result<(), CompiledSelectorProblemBuildError> {
    let pattern = required_string_term_const(pattern, "ast_string_literal_matching_regex.pattern")?;
    let matching_nodes = Regex::new(&pattern)
        .map(|regex| {
            facts
                .iter()
                .filter_map(|(fact_node, value)| regex.is_match(value).then_some(*fact_node))
                .collect::<BTreeSet<_>>()
        })
        .unwrap_or_default();
    add_node_allowed_tuples(model, variables, node, &matching_nodes)
}

fn add_node_ordinal_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    node: &NodeTerm,
    ordinal: &OrdinalTerm,
    facts: &BTreeSet<(NodeId, StatementOrdinal)>,
) -> Result<(), CompiledSelectorProblemBuildError> {
    let mut constraint_variables = Vec::new();
    if let NodeTerm::Var { id } = node {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if let OrdinalTerm::Var { id } = ordinal {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if constraint_variables.is_empty() {
        return facts
            .iter()
            .any(|(fact_node, fact_ordinal)| {
                node_term_matches(node, *fact_node) && ordinal_term_matches(ordinal, *fact_ordinal)
            })
            .then_some(())
            .ok_or_else(
                || CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied {
                    atom: format!("node/ordinal fact {node:?} {ordinal:?}"),
                },
            );
    }

    let tuples = facts
        .iter()
        .filter(|(fact_node, fact_ordinal)| {
            node_term_matches(node, *fact_node) && ordinal_term_matches(ordinal, *fact_ordinal)
        })
        .map(|(fact_node, fact_ordinal)| {
            let mut tuple = Vec::with_capacity(constraint_variables.len());
            if matches!(node, NodeTerm::Var { .. }) {
                tuple.push(model.intern_ast_node(*fact_node)?);
            }
            if matches!(ordinal, OrdinalTerm::Var { .. }) {
                tuple.push(model.intern_statement_ordinal(*fact_ordinal)?);
            }
            Ok(tuple)
        })
        .collect::<Result<Vec<_>, CompiledSelectorProblemError>>()?;

    add_encoded_allowed_tuple_set(model, constraint_variables, tuples)
}

fn add_ordinal_offset_constraint(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    base: &OrdinalTerm,
    ordinal: &OrdinalTerm,
    offset: i32,
) -> Result<(), CompiledSelectorProblemBuildError> {
    let base = ordinal_linear_variable(model, variables, base)?;
    let ordinal = ordinal_linear_variable(model, variables, ordinal)?;
    add_linear_offset_equality(model, base, ordinal, i64::from(offset))
}

fn add_ordinal_before_constraint(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    before: &OrdinalTerm,
    after: &OrdinalTerm,
) -> Result<(), CompiledSelectorProblemBuildError> {
    let before = ordinal_linear_variable(model, variables, before)?;
    let after = ordinal_linear_variable(model, variables, after)?;
    add_linear_offset_less_or_equal(model, before, after, 1)
}

fn ordinal_linear_variable(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    term: &OrdinalTerm,
) -> Result<ConstraintVariableId, CompiledSelectorProblemBuildError> {
    match term {
        OrdinalTerm::Var { id } => model_variable(variables, *id),
        OrdinalTerm::Const { ordinal } => {
            let value = model.intern_statement_ordinal(*ordinal)?;
            model
                .add_internal_integer_variable(
                    Some(format!("ordinal_const.{}", ordinal.0)),
                    std::iter::once(value),
                )
                .map_err(Into::into)
        }
    }
}

fn add_linear_offset_equality(
    model: &mut CompiledSelectorProblemBuilder,
    left: ConstraintVariableId,
    right: ConstraintVariableId,
    offset: i64,
) -> Result<(), CompiledSelectorProblemBuildError> {
    model
        .add_linear_constraint(vec![left, right], vec![1, -1], offset, vec![0, 0])
        .map_err(Into::into)
}

fn add_linear_offset_less_or_equal(
    model: &mut CompiledSelectorProblemBuilder,
    left: ConstraintVariableId,
    right: ConstraintVariableId,
    offset: i64,
) -> Result<(), CompiledSelectorProblemBuildError> {
    let lower_bound = linear_offset_less_or_equal_lower_bound(model, left, right, offset)?;
    model
        .add_linear_constraint(vec![left, right], vec![1, -1], offset, vec![lower_bound, 0])
        .map_err(Into::into)
}

fn linear_offset_less_or_equal_lower_bound(
    model: &CompiledSelectorProblemBuilder,
    left: ConstraintVariableId,
    right: ConstraintVariableId,
    offset: i64,
) -> Result<i64, CompiledSelectorProblemBuildError> {
    let left_values = model.variable_domain_values(left)?;
    let right_values = model.variable_domain_values(right)?;
    let Some(min_left) = left_values.first() else {
        return Ok(0);
    };
    let Some(max_right) = right_values.last() else {
        return Ok(0);
    };
    let lower = i128::from(min_left.0) - i128::from(max_right.0) + i128::from(offset);
    let lower = lower.min(0);
    i64::try_from(lower).map_err(|_| CompiledSelectorProblemBuildError::UnsupportedAtom {
        atom: "linear constraint lower bound exceeds i64 range".to_string(),
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum LoweredNodeTerm {
    Var(ConstraintVariableId),
    Const(NodeId),
}

struct ChildListPatternTerms<'a> {
    parent: &'a NodeTerm,
    start_index: u32,
    segments: &'a [Vec<NodeTerm>],
    anchored_left: bool,
    anchored_right: bool,
}

struct LoweredChildListPattern {
    parent: LoweredNodeTerm,
    start_index: u32,
    segments: Vec<Vec<LoweredNodeTerm>>,
    anchored_left: bool,
    anchored_right: bool,
}

impl LoweredChildListPattern {
    fn from_terms(
        terms: ChildListPatternTerms<'_>,
        variables: &[ConstraintVariableId],
    ) -> Result<Self, CompiledSelectorProblemBuildError> {
        let parent = lower_node_term(terms.parent, variables)?;
        let mut segments = Vec::with_capacity(terms.segments.len());
        for segment in terms.segments {
            let mut lowered_segment = Vec::with_capacity(segment.len());
            for child in segment {
                lowered_segment.push(lower_node_term(child, variables)?);
            }
            if !lowered_segment.is_empty() {
                segments.push(lowered_segment);
            }
        }
        Ok(Self {
            parent,
            start_index: terms.start_index,
            segments,
            anchored_left: terms.anchored_left,
            anchored_right: terms.anchored_right,
        })
    }
}

#[derive(Debug, Clone, Copy)]
struct ChildListSegmentPosition {
    start: ConstraintVariableId,
}

fn add_ast_child_list_pattern_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    terms: ChildListPatternTerms<'_>,
    domains: &FactDomains,
) -> Result<(), CompiledSelectorProblemBuildError> {
    let pattern = LoweredChildListPattern::from_terms(terms, variables)?;

    if pattern.segments.iter().all(Vec::is_empty) {
        return Ok(());
    }

    if pattern.segments.len() == 1 {
        return add_child_list_segment_constraint(model, &pattern, 0, None, domains);
    }

    let positions = add_child_list_segment_position_variables(model, &pattern, domains)?;
    for (segment_index, position) in positions.iter().copied().enumerate() {
        add_child_list_segment_constraint(model, &pattern, segment_index, Some(position), domains)?;
    }
    for segment_index in 1..pattern.segments.len() {
        add_linear_offset_less_or_equal(
            model,
            positions[segment_index - 1].start,
            positions[segment_index].start,
            child_list_segment_length(&pattern.segments[segment_index - 1])?,
        )?;
    }
    Ok(())
}

fn add_child_list_segment_position_variables(
    model: &mut CompiledSelectorProblemBuilder,
    pattern: &LoweredChildListPattern,
    domains: &FactDomains,
) -> Result<Vec<ChildListSegmentPosition>, CompiledSelectorProblemBuildError> {
    let index_values = child_list_index_domain_values(pattern, domains);
    let mut positions = Vec::with_capacity(pattern.segments.len());
    for segment_index in 0..pattern.segments.len() {
        let start = model.add_internal_integer_variable(
            Some(format!("ast_child_list.segment{segment_index}.start")),
            index_values.iter().copied(),
        )?;
        positions.push(ChildListSegmentPosition { start });
    }
    Ok(positions)
}

fn child_list_segment_length(
    segment: &[LoweredNodeTerm],
) -> Result<i64, CompiledSelectorProblemBuildError> {
    i64::try_from(segment.len()).map_err(|_| CompiledSelectorProblemBuildError::UnsupportedAtom {
        atom: "child-list segment length exceeds i64 range".to_string(),
    })
}

fn child_list_index_domain_values(
    pattern: &LoweredChildListPattern,
    domains: &FactDomains,
) -> Vec<BackendValueId> {
    let mut values = BTreeSet::new();
    for candidate_parent in
        child_list_candidate_parents(pattern.parent, &domains.ast_children_by_parent)
    {
        for (index, _child) in child_list_subject_children(
            &domains.ast_children_by_parent,
            candidate_parent,
            pattern.start_index,
        ) {
            values.insert(BackendValueId(i64::from(*index)));
        }
    }
    if values.is_empty() {
        return vec![BackendValueId(0)];
    }
    values.into_iter().collect()
}

fn add_child_list_segment_constraint(
    model: &mut CompiledSelectorProblemBuilder,
    pattern: &LoweredChildListPattern,
    segment_index: usize,
    position: Option<ChildListSegmentPosition>,
    domains: &FactDomains,
) -> Result<(), CompiledSelectorProblemBuildError> {
    let segment = &pattern.segments[segment_index];
    let mut constraint_variables = position
        .map(|position| vec![position.start])
        .unwrap_or_default();
    constraint_variables.extend(child_list_constraint_variables(
        std::iter::once(pattern.parent).chain(segment.iter().copied()),
    ));
    let mut tuples = Vec::new();
    let mut constant_only_match = false;

    for candidate_parent in
        child_list_candidate_parents(pattern.parent, &domains.ast_children_by_parent)
    {
        let subject_children = child_list_subject_children(
            &domains.ast_children_by_parent,
            candidate_parent,
            pattern.start_index,
        );
        let Some(latest_start) = subject_children.len().checked_sub(segment.len()) else {
            continue;
        };
        let mut lo = 0;
        let mut hi = latest_start;
        if segment_index == 0 && pattern.anchored_left {
            hi = 0;
        }
        if segment_index == pattern.segments.len() - 1 && pattern.anchored_right {
            lo = latest_start;
        }
        if lo > hi {
            continue;
        }

        for start in lo..=hi {
            let mut current = Vec::new();
            if let Some(position) = position {
                current.push((
                    position.start,
                    BackendValueId(i64::from(subject_children[start].0)),
                ));
            }
            if !bind_node_term(model, pattern.parent, candidate_parent, &mut current)? {
                continue;
            }
            let mut segment_matches = true;
            for (offset, term) in segment.iter().enumerate() {
                if !bind_node_term(
                    model,
                    *term,
                    subject_children[start + offset].1,
                    &mut current,
                )? {
                    segment_matches = false;
                    break;
                }
            }
            if segment_matches {
                finish_child_list_tuple(
                    &constraint_variables,
                    &current,
                    &mut tuples,
                    &mut constant_only_match,
                );
            }
        }
    }

    if constraint_variables.is_empty() {
        return constant_only_match.then_some(()).ok_or_else(|| {
            CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied {
                atom: "ast_child_list_pattern".to_string(),
            }
        });
    }

    add_encoded_allowed_tuple_set(model, constraint_variables, tuples)
}

fn child_list_constraint_variables<I>(terms: I) -> Vec<ConstraintVariableId>
where
    I: IntoIterator<Item = LoweredNodeTerm>,
{
    let mut variables = Vec::new();
    let mut seen = BTreeSet::new();
    for term in terms {
        if let LoweredNodeTerm::Var(variable) = term
            && seen.insert(variable)
        {
            variables.push(variable);
        }
    }
    variables
}

fn lower_node_term(
    term: &NodeTerm,
    variables: &[ConstraintVariableId],
) -> Result<LoweredNodeTerm, CompiledSelectorProblemBuildError> {
    match term {
        NodeTerm::Var { id } => model_variable(variables, *id).map(LoweredNodeTerm::Var),
        NodeTerm::Const { node } => Ok(LoweredNodeTerm::Const(*node)),
    }
}

fn child_list_candidate_parents(
    parent: LoweredNodeTerm,
    ast_children_by_parent: &BTreeMap<NodeId, Vec<(u32, NodeId)>>,
) -> Box<dyn Iterator<Item = NodeId> + '_> {
    match parent {
        LoweredNodeTerm::Const(node) => Box::new(std::iter::once(node)),
        LoweredNodeTerm::Var(_) => Box::new(ast_children_by_parent.keys().copied()),
    }
}

fn child_list_subject_children(
    ast_children_by_parent: &BTreeMap<NodeId, Vec<(u32, NodeId)>>,
    parent: NodeId,
    start_index: u32,
) -> &[(u32, NodeId)] {
    let Some(children) = ast_children_by_parent.get(&parent) else {
        return &[];
    };
    let start = children.partition_point(|(index, _child)| *index < start_index);
    &children[start..]
}

fn bind_node_term(
    model: &mut CompiledSelectorProblemBuilder,
    term: LoweredNodeTerm,
    actual: NodeId,
    current: &mut Vec<(ConstraintVariableId, BackendValueId)>,
) -> Result<bool, CompiledSelectorProblemError> {
    match term {
        LoweredNodeTerm::Var(variable) => {
            current.push((variable, model.intern_ast_node(actual)?));
            Ok(true)
        }
        LoweredNodeTerm::Const(expected) => Ok(expected == actual),
    }
}

fn finish_child_list_tuple(
    constraint_variables: &[ConstraintVariableId],
    current: &[(ConstraintVariableId, BackendValueId)],
    tuples: &mut Vec<Vec<BackendValueId>>,
    constant_only_match: &mut bool,
) {
    if constraint_variables.is_empty() {
        *constant_only_match = true;
        return;
    }
    let mut tuple = Vec::with_capacity(constraint_variables.len());
    for variable in constraint_variables {
        let mut bound_value = None;
        for (current_variable, current_value) in current {
            if current_variable != variable {
                continue;
            }
            match bound_value {
                Some(existing) if existing != *current_value => {
                    return;
                }
                Some(_) => {}
                None => bound_value = Some(*current_value),
            }
        }
        let Some(value) = bound_value else {
            return;
        };
        tuple.push(value);
    }
    tuples.push(tuple);
}

fn add_ast_bare_property_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    node: &NodeTerm,
    key: &StringTerm,
    identifier: &StringTerm,
    is_binding: bool,
    facts: &BTreeSet<(NodeId, String, String, bool)>,
) -> Result<(), CompiledSelectorProblemBuildError> {
    let mut constraint_variables = Vec::new();
    if let NodeTerm::Var { id } = node {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if let StringTerm::Var { id } = key {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if let StringTerm::Var { id } = identifier {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if constraint_variables.is_empty() {
        return facts
            .iter()
            .any(|(fact_node, fact_key, fact_identifier, fact_is_binding)| {
                *fact_is_binding == is_binding
                    && node_term_matches(node, *fact_node)
                    && string_term_matches(key, fact_key)
                    && string_term_matches(identifier, fact_identifier)
            })
            .then_some(())
            .ok_or_else(
                || CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied {
                    atom: format!(
                        "ast_bare_property fact {node:?} {key:?} {identifier:?} {is_binding}"
                    ),
                },
            );
    }

    let tuples = facts
        .iter()
        .filter(|(fact_node, fact_key, fact_identifier, fact_is_binding)| {
            *fact_is_binding == is_binding
                && node_term_matches(node, *fact_node)
                && string_term_matches(key, fact_key)
                && string_term_matches(identifier, fact_identifier)
        })
        .map(|(fact_node, fact_key, fact_identifier, _)| {
            let mut tuple = Vec::with_capacity(constraint_variables.len());
            if matches!(node, NodeTerm::Var { .. }) {
                tuple.push(model.intern_ast_node(*fact_node)?);
            }
            if matches!(key, StringTerm::Var { .. }) {
                tuple.push(model.intern_string(fact_key)?);
            }
            if matches!(identifier, StringTerm::Var { .. }) {
                tuple.push(model.intern_string(fact_identifier)?);
            }
            Ok(tuple)
        })
        .collect::<Result<Vec<_>, CompiledSelectorProblemError>>()?;

    add_encoded_allowed_tuple_set(model, constraint_variables, tuples)
}

fn add_ast_regex_literal_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    node: &NodeTerm,
    pattern: &StringTerm,
    flags: &StringTerm,
    facts: &BTreeSet<(NodeId, String, String)>,
) -> Result<(), CompiledSelectorProblemBuildError> {
    let mut constraint_variables = Vec::new();
    if let NodeTerm::Var { id } = node {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if let StringTerm::Var { id } = pattern {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if let StringTerm::Var { id } = flags {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if constraint_variables.is_empty() {
        return facts
            .iter()
            .any(|(fact_node, fact_pattern, fact_flags)| {
                node_term_matches(node, *fact_node)
                    && string_term_matches(pattern, fact_pattern)
                    && string_term_matches(flags, fact_flags)
            })
            .then_some(())
            .ok_or_else(
                || CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied {
                    atom: format!("ast_regex_literal fact {node:?} {pattern:?} {flags:?}"),
                },
            );
    }

    let tuples = facts
        .iter()
        .filter(|(fact_node, fact_pattern, fact_flags)| {
            node_term_matches(node, *fact_node)
                && string_term_matches(pattern, fact_pattern)
                && string_term_matches(flags, fact_flags)
        })
        .map(|(fact_node, fact_pattern, fact_flags)| {
            let mut tuple = Vec::with_capacity(constraint_variables.len());
            if matches!(node, NodeTerm::Var { .. }) {
                tuple.push(model.intern_ast_node(*fact_node)?);
            }
            if matches!(pattern, StringTerm::Var { .. }) {
                tuple.push(model.intern_string(fact_pattern)?);
            }
            if matches!(flags, StringTerm::Var { .. }) {
                tuple.push(model.intern_string(fact_flags)?);
            }
            Ok(tuple)
        })
        .collect::<Result<Vec<_>, CompiledSelectorProblemError>>()?;

    add_encoded_allowed_tuple_set(model, constraint_variables, tuples)
}

fn add_encoded_allowed_tuple_set(
    model: &mut CompiledSelectorProblemBuilder,
    variables: Vec<ConstraintVariableId>,
    tuples: Vec<Vec<BackendValueId>>,
) -> Result<(), CompiledSelectorProblemBuildError> {
    let (variables, tuples) = normalize_encoded_allowed_tuple_columns(variables, tuples);
    model
        .add_encoded_allowed_tuples(variables, tuples)
        .map(|_| ())
        .map_err(Into::into)
}

fn normalize_encoded_allowed_tuple_columns(
    variables: Vec<ConstraintVariableId>,
    tuples: Vec<Vec<BackendValueId>>,
) -> (Vec<ConstraintVariableId>, Vec<Vec<BackendValueId>>) {
    let mut unique_variables = Vec::new();
    let mut column_by_variable = BTreeMap::new();
    let mut output_column_by_input = Vec::with_capacity(variables.len());
    for variable in &variables {
        let output_column = if let Some(output_column) = column_by_variable.get(variable) {
            *output_column
        } else {
            let output_column = unique_variables.len();
            unique_variables.push(*variable);
            column_by_variable.insert(*variable, output_column);
            output_column
        };
        output_column_by_input.push(output_column);
    }

    if unique_variables.len() == variables.len() {
        return (variables, tuples);
    }

    let mut normalized_tuples = HashSet::new();
    'row: for tuple in tuples {
        let mut normalized = vec![None; unique_variables.len()];
        for (value, output_column) in tuple
            .into_iter()
            .zip(output_column_by_input.iter().copied())
        {
            if let Some(existing) = &normalized[output_column] {
                if existing != &value {
                    continue 'row;
                }
            } else {
                normalized[output_column] = Some(value);
            }
        }
        normalized_tuples.insert(
            normalized
                .into_iter()
                .map(|value| value.expect("every output column is referenced"))
                .collect(),
        );
    }

    (unique_variables, normalized_tuples.into_iter().collect())
}

fn owner_term_matches(term: &OwnerTerm, owner: OwnerId) -> bool {
    match term {
        OwnerTerm::Var { .. } => true,
        OwnerTerm::Const {
            owner: expected_owner,
        } => *expected_owner == owner,
    }
}

fn node_term_matches(term: &NodeTerm, node: NodeId) -> bool {
    match term {
        NodeTerm::Var { .. } => true,
        NodeTerm::Const {
            node: expected_node,
        } => *expected_node == node,
    }
}

fn string_term_matches(term: &StringTerm, value: &str) -> bool {
    match term {
        StringTerm::Var { .. } => true,
        StringTerm::Const {
            value: expected_value,
        } => expected_value == value,
    }
}

fn ordinal_term_matches(term: &OrdinalTerm, ordinal: StatementOrdinal) -> bool {
    match term {
        OrdinalTerm::Var { .. } => true,
        OrdinalTerm::Const {
            ordinal: expected_ordinal,
        } => *expected_ordinal == ordinal,
    }
}

fn optional_string_matches(expected: &Option<String>, actual: &Option<String>) -> bool {
    match expected {
        Some(expected) => actual.as_deref() == Some(expected.as_str()),
        None => true,
    }
}

fn optional_u32_matches(expected: Option<u32>, actual: usize) -> bool {
    match expected {
        Some(expected) => usize::try_from(expected).ok() == Some(actual),
        None => true,
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
struct FactDomains {
    owners: BTreeSet<OwnerId>,
    nodes: BTreeSet<NodeId>,
    strings: BTreeSet<String>,
    ordinals: BTreeSet<StatementOrdinal>,
    owner_kinds: BTreeSet<(OwnerId, String)>,
    owner_statement_ordinals: BTreeSet<(OwnerId, StatementOrdinal)>,
    owner_top_level_roots: BTreeSet<(OwnerId, NodeId)>,
    declared_bindings: BTreeSet<(OwnerId, String)>,
    export_names: BTreeSet<(OwnerId, String)>,
    raw_owner_references_binding: BTreeSet<(OwnerId, String, String)>,
    owner_references_binding: BTreeSet<(OwnerId, String, String)>,
    references_owner: BTreeSet<(OwnerId, OwnerId)>,
    aliases_owner: BTreeSet<(OwnerId, OwnerId)>,
    ast_kinds: BTreeSet<(NodeId, String)>,
    ast_children_by_parent: BTreeMap<NodeId, Vec<(u32, NodeId)>>,
    ast_child_counts: BTreeSet<(NodeId, u32)>,
    ast_super_classes: BTreeSet<(NodeId, NodeId)>,
    ast_string_literals: BTreeSet<(NodeId, String)>,
    ast_number_literals: BTreeSet<(NodeId, String)>,
    ast_bool_literals: BTreeSet<(NodeId, bool)>,
    ast_identifier_names: BTreeSet<(NodeId, String)>,
    ast_property_names: BTreeSet<(NodeId, String)>,
    ast_bare_properties: BTreeSet<(NodeId, String, String, bool)>,
    ast_operators: BTreeSet<(NodeId, String)>,
    ast_regex_literals: BTreeSet<(NodeId, String, String)>,
    ast_top_levels: BTreeSet<(NodeId, StatementOrdinal)>,
    ast_top_level_positions: BTreeSet<(NodeId, StatementOrdinal)>,
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
}

impl FactDomains {
    fn from_program_and_facts(program: &SelectorProgram, facts: &SelectorFactStore) -> Self {
        let mut domains = Self::default();
        domains.add_facts(facts);
        domains.finalize_indexes();
        domains.add_derived_facts();
        domains.add_program_constants(program);
        domains
    }

    fn summary(&self) -> SelectorModelBuildSummary {
        SelectorModelBuildSummary {
            domain_value_counts: BTreeMap::from([
                ("owner", self.owners.len()),
                ("ast_node", self.nodes.len()),
                ("string", self.strings.len()),
                ("statement_ordinal", self.ordinals.len()),
            ]),
            stored_relation_counts: BTreeMap::from([
                ("owner_kind", self.owner_kinds.len()),
                (
                    "owner_statement_ordinal",
                    self.owner_statement_ordinals.len(),
                ),
                ("owner_top_level_root", self.owner_top_level_roots.len()),
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
                ("ast_kind", self.ast_kinds.len()),
                ("ast_child", ast_child_count(&self.ast_children_by_parent)),
                ("ast_child_parent", self.ast_children_by_parent.len()),
                ("ast_child_count", self.ast_child_counts.len()),
                ("ast_super_class", self.ast_super_classes.len()),
                ("ast_string_literal", self.ast_string_literals.len()),
                ("ast_number_literal", self.ast_number_literals.len()),
                ("ast_bool_literal", self.ast_bool_literals.len()),
                ("ast_identifier_name", self.ast_identifier_names.len()),
                ("ast_property_name", self.ast_property_names.len()),
                ("ast_bare_property", self.ast_bare_properties.len()),
                ("ast_operator", self.ast_operators.len()),
                ("ast_regex_literal", self.ast_regex_literals.len()),
                ("ast_top_level", self.ast_top_levels.len()),
                ("ast_top_level_position", self.ast_top_level_positions.len()),
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
                ("owner_top_level_root", self.owner_top_level_roots.len()),
                (
                    "owner_references_binding",
                    self.owner_references_binding.len(),
                ),
                ("references_owner", self.references_owner.len()),
                ("aliases_owner", self.aliases_owner.len()),
                ("ast_child_count", self.ast_child_counts.len()),
                ("ast_top_level_position", self.ast_top_level_positions.len()),
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
            VariableDomain::AstNode => self
                .nodes
                .iter()
                .copied()
                .map(ConstraintValue::AstNode)
                .collect(),
            VariableDomain::String => self
                .strings
                .iter()
                .cloned()
                .map(ConstraintValue::String)
                .collect(),
            VariableDomain::StatementOrdinal => self
                .ordinals
                .iter()
                .copied()
                .map(ConstraintValue::StatementOrdinal)
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
                    self.add_ordinal(*statement_ordinal);
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
                SelectorFact::AstKind {
                    node, node_kind, ..
                } => {
                    self.add_node(*node);
                    self.ast_kinds
                        .insert((*node, node_kind.as_tag().to_string()));
                }
                SelectorFact::AstStringLiteral { node, value, .. } => {
                    self.add_node(*node);
                    self.ast_string_literals.insert((*node, value.clone()));
                }
                SelectorFact::AstNumberLiteral { node, value, .. } => {
                    self.add_node(*node);
                    self.ast_number_literals.insert((*node, value.clone()));
                }
                SelectorFact::AstBoolLiteral { node, value, .. } => {
                    self.add_node(*node);
                    self.ast_bool_literals.insert((*node, *value));
                }
                SelectorFact::AstIdentifierName { node, value, .. } => {
                    self.add_node(*node);
                    self.ast_identifier_names.insert((*node, value.clone()));
                }
                SelectorFact::AstPropertyName { node, value, .. } => {
                    self.add_node(*node);
                    self.ast_property_names.insert((*node, value.clone()));
                }
                SelectorFact::AstBareProperty {
                    node,
                    key,
                    identifier,
                    is_binding,
                    ..
                } => {
                    self.add_node(*node);
                    self.ast_bare_properties.insert((
                        *node,
                        key.clone(),
                        identifier.clone(),
                        *is_binding,
                    ));
                }
                SelectorFact::AstOperator { node, value, .. } => {
                    self.add_node(*node);
                    self.ast_operators.insert((*node, value.clone()));
                }
                SelectorFact::AstRegexLiteral {
                    node,
                    pattern,
                    flags,
                    ..
                } => {
                    self.add_node(*node);
                    self.ast_regex_literals
                        .insert((*node, pattern.clone(), flags.clone()));
                }
                SelectorFact::AstTopLevel {
                    node,
                    statement_ordinal,
                    ..
                } => {
                    self.add_node(*node);
                    self.ast_top_levels.insert((*node, *statement_ordinal));
                }
                SelectorFact::AstChild {
                    parent,
                    index,
                    child,
                    ..
                } => {
                    self.add_node(*parent);
                    self.add_node(*child);
                    self.ast_children_by_parent
                        .entry(*parent)
                        .or_default()
                        .push((*index, *child));
                }
                SelectorFact::AstSuperClass {
                    class_node,
                    super_class,
                    ..
                } => {
                    self.add_node(*class_node);
                    self.add_node(*super_class);
                    self.ast_super_classes.insert((*class_node, *super_class));
                }
                SelectorFact::MemberRead {
                    statement_ordinal,
                    object,
                    member,
                    ..
                } => {
                    self.add_ordinal(*statement_ordinal);
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
                    self.add_ordinal(*statement_ordinal);
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

            self.add_fact_strings(fact);
            self.add_fact_ordinals(fact);
        }
    }

    fn finalize_indexes(&mut self) {
        for children in self.ast_children_by_parent.values_mut() {
            children.sort_unstable();
            children.dedup();
        }
    }

    fn add_derived_facts(&mut self) {
        let mut owners_with_declarations = BTreeSet::new();
        let mut owners_by_binding: BTreeMap<String, BTreeSet<OwnerId>> = BTreeMap::new();
        for (owner, binding) in &self.declared_bindings {
            owners_with_declarations.insert(*owner);
            owners_by_binding
                .entry(binding.clone())
                .or_default()
                .insert(*owner);
        }
        let mut top_level_nodes_by_ordinal: BTreeMap<StatementOrdinal, Vec<NodeId>> =
            BTreeMap::new();
        for (node, ordinal) in &self.ast_top_levels {
            top_level_nodes_by_ordinal
                .entry(*ordinal)
                .or_default()
                .push(*node);
        }
        let mut top_levels = self.ast_top_levels.iter().copied().collect::<Vec<_>>();
        top_levels.sort_by_key(|(node, ordinal)| (*ordinal, *node));
        for (position, (node, _ordinal)) in top_levels.into_iter().enumerate() {
            let position = StatementOrdinal(position);
            self.add_ordinal(position);
            self.ast_top_level_positions.insert((node, position));
        }
        let mut raw_referencers_by_binding: BTreeMap<&str, Vec<OwnerId>> = BTreeMap::new();
        for (referencer, binding, _edge_kind) in &self.raw_owner_references_binding {
            raw_referencers_by_binding
                .entry(binding.as_str())
                .or_default()
                .push(*referencer);
        }

        for (owner, binding, edge_kind) in &self.raw_owner_references_binding {
            if owners_with_declarations.contains(owner) {
                self.owner_references_binding
                    .insert((*owner, binding.clone(), edge_kind.clone()));
            }
        }

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

        let owner_by_ordinal = self
            .owner_statement_ordinals
            .iter()
            .map(|(owner, ordinal)| (*ordinal, *owner))
            .collect::<BTreeMap<_, _>>();
        for (statement_ordinal, object, member) in &self.raw_member_reads {
            let Some(owner) = owner_by_ordinal.get(statement_ordinal) else {
                continue;
            };
            if !owners_with_declarations.contains(owner) {
                continue;
            }
            self.member_reads.insert((*owner, member.clone()));
            if let Some(object) = object {
                self.member_reads_from_binding
                    .insert((*owner, object.clone(), member.clone()));
            }
        }

        for (owner, object_binding, member) in &self.member_reads_from_binding {
            if let Some(object_owners) = owners_by_binding.get(object_binding) {
                self.reads_member_of_owner.extend(
                    object_owners
                        .iter()
                        .map(|object_owner| (*owner, *object_owner, member.clone())),
                );
            }
        }

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

        for (argument, callee_object, callee_member, arg_index) in &self.raw_call_arguments {
            let Some(argument_owners) = owners_by_binding.get(argument) else {
                continue;
            };
            for owner in argument_owners {
                self.call_arguments
                    .insert((*owner, callee_member.clone(), *arg_index));
                if let Some(callee_object) = callee_object {
                    self.call_arguments_from_binding.insert((
                        *owner,
                        callee_object.clone(),
                        callee_member.clone(),
                        *arg_index,
                    ));
                    if let Some(callee_object_owners) = owners_by_binding.get(callee_object) {
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

        for (callee, class_anchor, member) in &self.decorate_calls {
            let Some(callee_owners) = owners_by_binding.get(callee) else {
                continue;
            };
            for owner in callee_owners {
                self.makes_decorate_call_for_binding.insert((
                    *owner,
                    class_anchor.clone(),
                    member.clone(),
                ));
                if let Some(class_owners) = owners_by_binding.get(class_anchor) {
                    self.makes_decorate_call_for_owner.extend(
                        class_owners
                            .iter()
                            .map(|class_owner| (*owner, *class_owner, member.clone())),
                    );
                }
            }
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

        let mut child_counts = self
            .nodes
            .iter()
            .map(|node| (*node, 0))
            .collect::<BTreeMap<_, _>>();
        for (parent, children) in &self.ast_children_by_parent {
            let count = child_counts.entry(*parent).or_insert(0);
            for (index, _child) in children {
                *count = (*count).max(index + 1);
            }
        }
        self.ast_child_counts
            .extend(child_counts.into_iter().collect::<BTreeSet<_>>());

        for (owner, ordinal) in &self.owner_statement_ordinals {
            if let Some(nodes) = top_level_nodes_by_ordinal.get(ordinal) {
                self.owner_top_level_roots
                    .extend(nodes.iter().map(|node| (*owner, *node)));
            }
        }

        let binding_ident_nodes = self
            .ast_kinds
            .iter()
            .filter_map(|(node, kind)| {
                (kind == chunk_facts::NodeKind::BindingIdent.as_tag()).then_some(*node)
            })
            .collect::<BTreeSet<_>>();
        let identifier_by_node = self
            .ast_identifier_names
            .iter()
            .map(|(node, value)| (*node, value.as_str()))
            .collect::<BTreeMap<_, _>>();
        for (root, _ordinal) in &self.ast_top_levels {
            let mut stack = vec![*root];
            while let Some(node) = stack.pop() {
                if binding_ident_nodes.contains(&node)
                    && let Some(binding) = identifier_by_node.get(&node)
                    && let Some(owners) = owners_by_binding.get(*binding)
                {
                    self.owner_top_level_roots
                        .extend(owners.iter().map(|owner| (*owner, *root)));
                }
                if let Some(children) = self.ast_children_by_parent.get(&node) {
                    stack.extend(children.iter().map(|(_index, child)| *child));
                }
            }
        }
    }

    fn add_fact_strings(&mut self, fact: &SelectorFact) {
        match fact {
            SelectorFact::AstStringLiteral { value, .. }
            | SelectorFact::AstNumberLiteral { value, .. }
            | SelectorFact::AstIdentifierName { value, .. }
            | SelectorFact::AstPropertyName { value, .. }
            | SelectorFact::AstOperator { value, .. } => self.add_string(value),
            SelectorFact::AstBareProperty {
                key, identifier, ..
            } => {
                self.add_string(key);
                self.add_string(identifier);
            }
            SelectorFact::AstRegexLiteral { pattern, flags, .. } => {
                self.add_string(pattern);
                self.add_string(flags);
            }
            _ => {}
        }
    }

    fn add_fact_ordinals(&mut self, fact: &SelectorFact) {
        if let SelectorFact::AstTopLevel {
            statement_ordinal, ..
        } = fact
        {
            self.add_ordinal(*statement_ordinal);
        }
    }

    fn add_program_constants(&mut self, program: &SelectorProgram) {
        for target in &program.targets {
            match &target.claim {
                ClaimKind::Binding {
                    export_name: Some(export_name),
                }
                | ClaimKind::BindingGroupMember { export_name } => self.add_string(export_name),
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
            SelectorAtom::OwnerStatementOrdinal { owner, ordinal } => {
                self.add_owner_term(owner);
                self.add_ordinal_term(ordinal);
            }
            SelectorAtom::OwnerTopLevelRoot { owner, root } => {
                self.add_owner_term(owner);
                self.add_node_term(root);
            }
            SelectorAtom::OwnerDeclaresBinding { owner, binding } => {
                self.add_owner_term(owner);
                self.add_string_term(binding);
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
            SelectorAtom::AstKind { node, .. }
            | SelectorAtom::AstChildCount { node, .. }
            | SelectorAtom::AstBoolLiteral { node, .. } => self.add_node_term(node),
            SelectorAtom::AstChild { parent, child, .. } => {
                self.add_node_term(parent);
                self.add_node_term(child);
            }
            SelectorAtom::AstChildListPattern {
                parent, segments, ..
            } => {
                self.add_node_term(parent);
                for segment in segments {
                    for child in segment {
                        self.add_node_term(child);
                    }
                }
            }
            SelectorAtom::AstSuperClass {
                class_node,
                super_class,
            } => {
                self.add_node_term(class_node);
                self.add_node_term(super_class);
            }
            SelectorAtom::AstStringLiteral { node, value }
            | SelectorAtom::AstStringLiteralMatchingRegex {
                node,
                pattern: value,
            }
            | SelectorAtom::AstNumberLiteral { node, value }
            | SelectorAtom::AstIdentifierName { node, value }
            | SelectorAtom::AstPropertyName { node, value }
            | SelectorAtom::AstOperator { node, value } => {
                self.add_node_term(node);
                self.add_string_term(value);
            }
            SelectorAtom::AstBareProperty {
                node,
                key,
                identifier,
                ..
            } => {
                self.add_node_term(node);
                self.add_string_term(key);
                self.add_string_term(identifier);
            }
            SelectorAtom::AstRegexLiteral {
                node,
                pattern,
                flags,
            } => {
                self.add_node_term(node);
                self.add_string_term(pattern);
                self.add_string_term(flags);
            }
            SelectorAtom::AstTopLevel { node, ordinal } => {
                self.add_node_term(node);
                self.add_ordinal_term(ordinal);
            }
            SelectorAtom::OrdinalOffset { base, ordinal, .. }
            | SelectorAtom::OrdinalBefore {
                before: base,
                after: ordinal,
            } => {
                self.add_ordinal_term(base);
                self.add_ordinal_term(ordinal);
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
            SelectorAtom::Equal { .. } | SelectorAtom::NotEqual { .. } => {}
        }
    }

    fn add_owner_term(&mut self, term: &OwnerTerm) {
        if let OwnerTerm::Const { owner } = term {
            self.add_owner(*owner);
        }
    }

    fn add_node_term(&mut self, term: &NodeTerm) {
        if let NodeTerm::Const { node } = term {
            self.add_node(*node);
        }
    }

    fn add_string_term(&mut self, term: &StringTerm) {
        if let StringTerm::Const { value } = term {
            self.add_string(value);
        }
    }

    fn add_ordinal_term(&mut self, term: &OrdinalTerm) {
        if let OrdinalTerm::Const { ordinal } = term {
            self.add_ordinal(*ordinal);
        }
    }

    fn add_owner(&mut self, owner: OwnerId) {
        self.owners.insert(owner);
    }

    fn add_node(&mut self, node: NodeId) {
        self.nodes.insert(node);
    }

    fn add_string(&mut self, value: &str) {
        self.strings.insert(value.to_string());
    }

    fn add_ordinal(&mut self, ordinal: StatementOrdinal) {
        self.ordinals.insert(ordinal);
    }
}

fn ast_child_count(ast_children_by_parent: &BTreeMap<NodeId, Vec<(u32, NodeId)>>) -> usize {
    ast_children_by_parent
        .values()
        .map(|children| children.len())
        .sum()
}

#[cfg(test)]
mod tests {
    use super::*;
    use analysis::{ChunkId, OwnerId, StatementOrdinal};
    use chunk_facts::NodeKind;
    use selector_constraint_backend::{
        AllDifferentConstraintId, AllDifferentReason, AllowedTupleConstraintId, BackendValueId,
        BinaryConstraintKind, CompiledAllDifferentConstraint as AllDifferentConstraint,
        CompiledBinaryConstraint as BinaryConstraint, CompiledLinearConstraint as LinearConstraint,
        ConstraintValue,
    };
    use selector_ir::{ClaimOrigin, SelectorTargetId};
    use selector_ir_lowering::{MemberSelectorLoweringContext, MemberSelectorProgramBuilder};
    use spec::{AnonymousStatementSelector, MemberSelectorSpec, SourceMatchIdentifierMode};

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

    fn ordinal(value: usize) -> ConstraintValue {
        ConstraintValue::StatementOrdinal(StatementOrdinal(value))
    }

    fn ast_node(value: u32) -> ConstraintValue {
        ConstraintValue::AstNode(value)
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

    fn ast_kind(node: u32, node_kind: NodeKind) -> SelectorFact {
        SelectorFact::AstKind {
            chunk_id: ChunkId(0),
            node,
            node_kind,
        }
    }

    fn ast_child(parent: u32, index: u32, child: u32) -> SelectorFact {
        SelectorFact::AstChild {
            chunk_id: ChunkId(0),
            parent,
            index,
            child,
        }
    }

    fn ast_string_literal(node: u32, value: &str) -> SelectorFact {
        SelectorFact::AstStringLiteral {
            chunk_id: ChunkId(0),
            node,
            value: value.to_string(),
        }
    }

    fn ast_identifier_name(node: u32, value: &str) -> SelectorFact {
        SelectorFact::AstIdentifierName {
            chunk_id: ChunkId(0),
            node,
            value: value.to_string(),
        }
    }

    fn ast_bare_property(node: u32, key: &str, identifier: &str, is_binding: bool) -> SelectorFact {
        SelectorFact::AstBareProperty {
            chunk_id: ChunkId(0),
            node,
            key: key.to_string(),
            identifier: identifier.to_string(),
            is_binding,
        }
    }

    fn ast_regex_literal(node: u32, pattern: &str, flags: &str) -> SelectorFact {
        SelectorFact::AstRegexLiteral {
            chunk_id: ChunkId(0),
            node,
            pattern: pattern.to_string(),
            flags: flags.to_string(),
        }
    }

    fn ast_super_class(class_node: u32, super_class: u32) -> SelectorFact {
        SelectorFact::AstSuperClass {
            chunk_id: ChunkId(0),
            class_node,
            super_class,
        }
    }

    fn ast_top_level(node: u32, ordinal: usize) -> SelectorFact {
        SelectorFact::AstTopLevel {
            chunk_id: ChunkId(0),
            node,
            statement_ordinal: StatementOrdinal(ordinal),
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
            tuples: constraint
                .tuples
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
                .is_some_and(|row| constraint.tuples.contains(&row))
        }) && model.binary_constraints.iter().all(|constraint| {
            let Some(left) = assignment.get(&constraint.left) else {
                return false;
            };
            let Some(right) = assignment.get(&constraint.right) else {
                return false;
            };
            match constraint.kind {
                BinaryConstraintKind::Equal => left == right,
                BinaryConstraintKind::NotEqual => left != right,
                BinaryConstraintKind::OrdinalBefore => left < right,
            }
        }) && model.linear_constraints.iter().all(|constraint| {
            let mut value = i128::from(constraint.offset);
            for (variable, coefficient) in constraint
                .variables
                .iter()
                .zip(constraint.coefficients.iter())
            {
                let Some(variable_value) = assignment.get(variable) else {
                    return false;
                };
                value += i128::from(variable_value.0) * i128::from(*coefficient);
            }
            constraint.domain.chunks_exact(2).any(|interval| {
                i128::from(interval[0]) <= value && value <= i128::from(interval[1])
            })
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
        let node = program.add_variable(VariableDomain::AstNode, Some("node".to_string()));
        program.add_atom(SelectorAtom::AstChild {
            parent: NodeTerm::Var { id: node },
            index: 0,
            child: NodeTerm::Var { id: node },
        });

        let facts = fact_store(vec![
            ast_child(10, 0, 10),
            ast_child(10, 1, 20),
            ast_child(20, 0, 20),
        ]);

        let model = compile_selector_problem(&program, &facts).unwrap();

        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(0)]),
            &AllowedTupleConstraint {
                id: AllowedTupleConstraintId(0),
                variables: vec![ConstraintVariableId(0)],
                tuples: vec![vec![ast_node(10)], vec![ast_node(20)]],
            }
        );
    }

    #[test]
    fn target_injectivity_preserves_broad_specific_shape() {
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
        assert_eq!(model.binary_constraints, Vec::<BinaryConstraint>::new());
        assert_eq!(
            model.all_different,
            vec![AllDifferentConstraint {
                id: AllDifferentConstraintId(0),
                variables: vec![ConstraintVariableId(0), ConstraintVariableId(1)],
                reason: AllDifferentReason::TargetInjectivity {
                    targets: vec![SelectorTargetId(0), SelectorTargetId(1)],
                },
            }]
        );

        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(0)]),
            &AllowedTupleConstraint {
                id: AllowedTupleConstraintId(0),
                variables: vec![ConstraintVariableId(0)],
                tuples: vec![vec![owner(10)], vec![owner(20)]],
            }
        );
        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(1)]),
            &AllowedTupleConstraint {
                id: AllowedTupleConstraintId(1),
                variables: vec![ConstraintVariableId(1)],
                tuples: vec![vec![owner(20)]],
            }
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
        program.add_atom(SelectorAtom::AstIdentifierName {
            node: NodeTerm::Const { node: 1 },
            value: StringTerm::Var { id: left },
        });
        program.add_atom(SelectorAtom::AstIdentifierName {
            node: NodeTerm::Const { node: 2 },
            value: StringTerm::Var { id: right },
        });
        program.require_variables_all_different(
            vec![left, right],
            "module::source_match.alpha_all.frame",
        );

        let facts = fact_store(vec![
            owner_fact(10, 0, "var"),
            declared_binding(10, "widget"),
            ast_identifier_name(1, "a"),
            ast_identifier_name(2, "b"),
        ]);

        let model = compile_selector_problem(&program, &facts).unwrap();

        assert_eq!(
            model.all_different,
            vec![AllDifferentConstraint {
                id: AllDifferentConstraintId(0),
                variables: vec![ConstraintVariableId(1), ConstraintVariableId(2)],
                reason: AllDifferentReason::SelectorSemantics {
                    label: "module::source_match.alpha_all.frame".to_string(),
                },
            }]
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
    fn ast_fact_atoms_lower_to_allowed_tuple_constraints() {
        let mut program = SelectorProgram::default();
        let owner_var = program.add_variable(VariableDomain::Owner, Some("owner".to_string()));
        let root_var = program.add_variable(VariableDomain::AstNode, Some("root".to_string()));
        let ident_node_var =
            program.add_variable(VariableDomain::AstNode, Some("ident_node".to_string()));
        let literal_node_var =
            program.add_variable(VariableDomain::AstNode, Some("literal_node".to_string()));
        let prop_node_var =
            program.add_variable(VariableDomain::AstNode, Some("prop_node".to_string()));
        let super_node_var =
            program.add_variable(VariableDomain::AstNode, Some("super_node".to_string()));
        let ordinal_var = program.add_variable(
            VariableDomain::StatementOrdinal,
            Some("ordinal".to_string()),
        );
        let next_ordinal_var =
            program.add_variable(VariableDomain::StatementOrdinal, Some("next".to_string()));
        let ident_var = program.add_variable(VariableDomain::String, Some("ident".to_string()));
        let regex_pattern_var =
            program.add_variable(VariableDomain::String, Some("regex_pattern".to_string()));

        program.add_atom(SelectorAtom::OwnerTopLevelRoot {
            owner: OwnerTerm::Var { id: owner_var },
            root: NodeTerm::Var { id: root_var },
        });
        program.add_atom(SelectorAtom::AstTopLevel {
            node: NodeTerm::Var { id: root_var },
            ordinal: OrdinalTerm::Var { id: ordinal_var },
        });
        program.add_atom(SelectorAtom::AstKind {
            node: NodeTerm::Var { id: root_var },
            node_kind: NodeKind::FnDecl,
        });
        program.add_atom(SelectorAtom::AstChild {
            parent: NodeTerm::Var { id: root_var },
            index: 0,
            child: NodeTerm::Var { id: ident_node_var },
        });
        program.add_atom(SelectorAtom::AstChild {
            parent: NodeTerm::Var { id: root_var },
            index: 1,
            child: NodeTerm::Var {
                id: literal_node_var,
            },
        });
        program.add_atom(SelectorAtom::AstChildCount {
            node: NodeTerm::Var { id: root_var },
            count: 2,
        });
        program.add_atom(SelectorAtom::AstIdentifierName {
            node: NodeTerm::Var { id: ident_node_var },
            value: StringTerm::Var { id: ident_var },
        });
        program.add_atom(SelectorAtom::AstStringLiteral {
            node: NodeTerm::Var {
                id: literal_node_var,
            },
            value: StringTerm::Const {
                value: "needle".to_string(),
            },
        });
        program.add_atom(SelectorAtom::AstBareProperty {
            node: NodeTerm::Var { id: prop_node_var },
            key: StringTerm::Const {
                value: "key".to_string(),
            },
            identifier: StringTerm::Var { id: ident_var },
            is_binding: true,
        });
        program.add_atom(SelectorAtom::AstRegexLiteral {
            node: NodeTerm::Var { id: prop_node_var },
            pattern: StringTerm::Var {
                id: regex_pattern_var,
            },
            flags: StringTerm::Const {
                value: "g".to_string(),
            },
        });
        program.add_atom(SelectorAtom::AstSuperClass {
            class_node: NodeTerm::Var { id: root_var },
            super_class: NodeTerm::Var { id: super_node_var },
        });
        program.add_atom(SelectorAtom::OrdinalOffset {
            base: OrdinalTerm::Var { id: ordinal_var },
            ordinal: OrdinalTerm::Var {
                id: next_ordinal_var,
            },
            offset: 1,
        });

        let facts = fact_store(vec![
            owner_fact(1, 0, "function"),
            owner_fact(2, 1, "function"),
            ast_top_level(100, 0),
            ast_top_level(200, 1),
            ast_kind(100, NodeKind::FnDecl),
            ast_kind(200, NodeKind::ClassDecl),
            ast_child(100, 0, 110),
            ast_child(100, 1, 120),
            ast_identifier_name(110, "selectedIdent"),
            ast_string_literal(120, "needle"),
            ast_bare_property(130, "key", "selectedIdent", true),
            ast_regex_literal(130, "^x", "g"),
            ast_super_class(100, 140),
        ]);

        let model = compile_selector_problem(&program, &facts).unwrap();

        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(0), ConstraintVariableId(1)]).tuples,
            vec![vec![owner(1), ast_node(100)], vec![owner(2), ast_node(200)]]
        );
        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(1), ConstraintVariableId(2)]).tuples,
            vec![vec![ast_node(100), ast_node(110)]]
        );
        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(2), ConstraintVariableId(8)]).tuples,
            vec![vec![ast_node(110), string("selectedIdent")]]
        );
        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(4), ConstraintVariableId(8)]).tuples,
            vec![vec![ast_node(130), string("selectedIdent")]]
        );
        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(4), ConstraintVariableId(9)]).tuples,
            vec![vec![ast_node(130), string("^x")]]
        );
        assert_eq!(
            model.linear_constraints,
            vec![LinearConstraint {
                variables: vec![ConstraintVariableId(6), ConstraintVariableId(7)],
                coefficients: vec![1, -1],
                offset: 1,
                domain: vec![0, 0],
            }]
        );
    }

    #[test]
    fn ast_top_level_constraints_use_dense_top_level_positions() {
        let mut program = SelectorProgram::default();
        let first_root = program.add_variable(VariableDomain::AstNode, Some("first".to_string()));
        let second_root = program.add_variable(VariableDomain::AstNode, Some("second".to_string()));
        let first_ordinal = program.add_variable(
            VariableDomain::StatementOrdinal,
            Some("first_ord".to_string()),
        );
        let second_ordinal = program.add_variable(
            VariableDomain::StatementOrdinal,
            Some("second_ord".to_string()),
        );
        program.add_atom(SelectorAtom::AstTopLevel {
            node: NodeTerm::Var { id: first_root },
            ordinal: OrdinalTerm::Var { id: first_ordinal },
        });
        program.add_atom(SelectorAtom::AstTopLevel {
            node: NodeTerm::Var { id: second_root },
            ordinal: OrdinalTerm::Var { id: second_ordinal },
        });
        program.add_atom(SelectorAtom::OrdinalOffset {
            base: OrdinalTerm::Var { id: first_ordinal },
            ordinal: OrdinalTerm::Var { id: second_ordinal },
            offset: 1,
        });
        let facts = fact_store(vec![ast_top_level(100, 0), ast_top_level(200, 3)]);

        let model = compile_selector_problem(&program, &facts).unwrap();

        assert_eq!(
            satisfying_tuples_for(&model, &[ConstraintVariableId(0), ConstraintVariableId(1)]),
            vec![vec![ast_node(100), ast_node(200)]]
        );
    }

    #[test]
    fn ast_string_literal_matching_regex_restricts_node_domain() {
        let mut program = SelectorProgram::default();
        let node = program.add_variable(VariableDomain::AstNode, Some("literal".to_string()));
        program.add_atom(SelectorAtom::AstStringLiteralMatchingRegex {
            node: NodeTerm::Var { id: node },
            pattern: StringTerm::Const {
                value: "^button-".to_string(),
            },
        });

        let facts = fact_store(vec![
            ast_string_literal(10, "button-primary"),
            ast_string_literal(20, "input-primary"),
            ast_string_literal(30, "button-secondary"),
        ]);

        let model = compile_selector_problem(&program, &facts).unwrap();

        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(0)]),
            &AllowedTupleConstraint {
                id: AllowedTupleConstraintId(0),
                variables: vec![ConstraintVariableId(0)],
                tuples: vec![vec![ast_node(10)], vec![ast_node(30)]],
            }
        );
    }

    #[test]
    fn ast_string_literal_matching_regex_rejects_variable_pattern() {
        let mut program = SelectorProgram::default();
        let node = program.add_variable(VariableDomain::AstNode, Some("literal".to_string()));
        let pattern = program.add_variable(VariableDomain::String, Some("pattern".to_string()));
        program.add_atom(SelectorAtom::AstStringLiteralMatchingRegex {
            node: NodeTerm::Var { id: node },
            pattern: StringTerm::Var { id: pattern },
        });

        let facts = fact_store(vec![ast_string_literal(10, "button-primary")]);

        let err = compile_selector_problem(&program, &facts).unwrap_err();
        assert!(matches!(
            err,
            CompiledSelectorProblemBuildError::UnsupportedAtom { .. }
        ));
    }

    #[test]
    fn ast_child_list_pattern_emits_ordered_segment_tuples() {
        let mut program = SelectorProgram::default();
        let parent = program.add_variable(VariableDomain::AstNode, Some("parent".to_string()));
        let left = program.add_variable(VariableDomain::AstNode, Some("left".to_string()));
        let right = program.add_variable(VariableDomain::AstNode, Some("right".to_string()));
        program.add_atom(SelectorAtom::AstChildListPattern {
            parent: NodeTerm::Var { id: parent },
            start_index: 0,
            segments: vec![
                vec![NodeTerm::Var { id: left }],
                vec![NodeTerm::Var { id: right }],
            ],
            anchored_left: false,
            anchored_right: false,
        });

        let facts = fact_store(vec![
            ast_child(100, 0, 10),
            ast_child(100, 1, 20),
            ast_child(100, 2, 30),
            ast_child(200, 0, 20),
            ast_child(200, 1, 10),
        ]);

        let model = compile_selector_problem(&program, &facts).unwrap();

        assert_eq!(
            model.linear_constraints,
            vec![LinearConstraint {
                variables: vec![ConstraintVariableId(3), ConstraintVariableId(4)],
                coefficients: vec![1, -1],
                offset: 1,
                domain: vec![-1, 0],
            }]
        );
        assert_eq!(
            satisfying_tuples_for(
                &model,
                &[
                    ConstraintVariableId(0),
                    ConstraintVariableId(1),
                    ConstraintVariableId(2)
                ]
            ),
            vec![
                vec![ast_node(100), ast_node(10), ast_node(20)],
                vec![ast_node(100), ast_node(10), ast_node(30)],
                vec![ast_node(100), ast_node(20), ast_node(30)],
                vec![ast_node(200), ast_node(20), ast_node(10)],
            ]
        );
    }

    #[test]
    fn ast_child_list_pattern_anchors_fixed_segments_to_edges() {
        let mut program = SelectorProgram::default();
        let parent = program.add_variable(VariableDomain::AstNode, Some("parent".to_string()));
        program.add_atom(SelectorAtom::AstChildListPattern {
            parent: NodeTerm::Var { id: parent },
            start_index: 0,
            segments: vec![
                vec![NodeTerm::Const { node: 10 }],
                vec![NodeTerm::Const { node: 30 }],
            ],
            anchored_left: true,
            anchored_right: true,
        });

        let facts = fact_store(vec![
            ast_child(100, 0, 10),
            ast_child(100, 1, 20),
            ast_child(100, 2, 30),
            ast_child(200, 0, 5),
            ast_child(200, 1, 10),
            ast_child(200, 2, 30),
            ast_child(300, 0, 10),
            ast_child(300, 1, 30),
            ast_child(300, 2, 40),
        ]);

        let model = compile_selector_problem(&program, &facts).unwrap();

        assert_eq!(
            satisfying_tuples_for(&model, &[ConstraintVariableId(0)]),
            vec![vec![ast_node(100)]]
        );
    }

    #[test]
    fn ast_child_list_pattern_start_index_rebases_left_anchor() {
        let mut program = SelectorProgram::default();
        let parent = program.add_variable(VariableDomain::AstNode, Some("parent".to_string()));
        let child = program.add_variable(VariableDomain::AstNode, Some("child".to_string()));
        program.add_atom(SelectorAtom::AstChildListPattern {
            parent: NodeTerm::Var { id: parent },
            start_index: 1,
            segments: vec![vec![NodeTerm::Var { id: child }]],
            anchored_left: true,
            anchored_right: false,
        });

        let facts = fact_store(vec![
            ast_child(100, 0, 10),
            ast_child(100, 1, 20),
            ast_child(100, 2, 30),
            ast_child(200, 0, 40),
        ]);

        let model = compile_selector_problem(&program, &facts).unwrap();

        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(0), ConstraintVariableId(1)]),
            &AllowedTupleConstraint {
                id: AllowedTupleConstraintId(0),
                variables: vec![ConstraintVariableId(0), ConstraintVariableId(1)],
                tuples: vec![vec![ast_node(100), ast_node(20)]],
            }
        );
    }

    #[test]
    fn ast_child_list_pattern_run_holes_do_not_impose_child_count() {
        let mut program = SelectorProgram::default();
        let parent = program.add_variable(VariableDomain::AstNode, Some("parent".to_string()));
        program.add_atom(SelectorAtom::AstChildListPattern {
            parent: NodeTerm::Var { id: parent },
            start_index: 0,
            segments: vec![vec![NodeTerm::Const { node: 20 }]],
            anchored_left: false,
            anchored_right: false,
        });

        let facts = fact_store(vec![
            ast_child(100, 0, 10),
            ast_child(100, 1, 20),
            ast_child(100, 2, 30),
            ast_child(200, 0, 20),
            ast_child(300, 0, 10),
            ast_child(300, 1, 30),
        ]);

        let model = compile_selector_problem(&program, &facts).unwrap();

        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(0)]),
            &AllowedTupleConstraint {
                id: AllowedTupleConstraintId(0),
                variables: vec![ConstraintVariableId(0)],
                tuples: vec![vec![ast_node(100)], vec![ast_node(200)]],
            }
        );
    }

    #[test]
    fn ast_child_list_pattern_all_holes_is_neutral() {
        let parent = SelectorVariableId(0);
        let mut model = CompiledSelectorProblemBuilder::default();
        model
            .add_full_domain_values(VariableDomain::AstNode, vec![ast_node(100), ast_node(200)])
            .unwrap();
        let parent_model_var = model
            .add_variable(parent, VariableDomain::AstNode, Some("parent".to_string()))
            .unwrap();
        let variables = vec![parent_model_var];
        let segments = Vec::new();
        let domains = FactDomains::default();

        add_ast_child_list_pattern_allowed_tuples(
            &mut model,
            &variables,
            ChildListPatternTerms {
                parent: &NodeTerm::Var { id: parent },
                start_index: 0,
                segments: &segments,
                anchored_left: false,
                anchored_right: false,
            },
            &domains,
        )
        .unwrap();

        let model = model.finish().unwrap();
        assert!(model.allowed_tuples.is_empty());
        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(0)),
            vec![ConstraintValue::AstNode(100), ConstraintValue::AstNode(200)]
        );
    }

    #[test]
    fn ast_child_list_pattern_merges_repeated_variables() {
        let mut program = SelectorProgram::default();
        let parent = program.add_variable(VariableDomain::AstNode, Some("parent".to_string()));
        let child = program.add_variable(VariableDomain::AstNode, Some("child".to_string()));
        program.add_atom(SelectorAtom::AstChildListPattern {
            parent: NodeTerm::Var { id: parent },
            start_index: 0,
            segments: vec![vec![
                NodeTerm::Var { id: child },
                NodeTerm::Var { id: child },
            ]],
            anchored_left: false,
            anchored_right: false,
        });

        let facts = fact_store(vec![
            ast_child(100, 0, 10),
            ast_child(100, 1, 10),
            ast_child(200, 0, 10),
            ast_child(200, 1, 20),
        ]);

        let model = compile_selector_problem(&program, &facts).unwrap();

        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(0), ConstraintVariableId(1)]),
            &AllowedTupleConstraint {
                id: AllowedTupleConstraintId(0),
                variables: vec![ConstraintVariableId(0), ConstraintVariableId(1)],
                tuples: vec![vec![ast_node(100), ast_node(10)]],
            }
        );
    }

    #[test]
    fn alpha_all_source_match_model_accepts_matching_chunk_facts() {
        js_ast::with_swc_globals(|| {
            let mut selector =
                AnonymousStatementSelector::exact("function readable(items) { return items; }");
            selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
            let lowered = selector_ir_lowering::lower_member_selector(
                &MemberSelectorLoweringContext::new(ChunkId(0), "static/app::format"),
                "format_items",
                &MemberSelectorSpec::SourceMatch(selector),
            )
            .unwrap();

            let module = js_ast::parse_js_module_ast(
                "<chunk>",
                "function actual(values) { return values; }\nexport { actual };\n",
            )
            .unwrap();
            let chunk_facts = chunk_facts::extract_facts(&module).unwrap();
            let mut facts = SelectorFactStore::default();
            facts.extend_chunk_facts(ChunkId(0), &chunk_facts);
            facts.push(owner_fact(0, 0, "fn_decl"));
            facts.push(declared_binding(0, "actual"));

            let model = compile_selector_problem(&lowered.program, &facts).unwrap();
            let empty_constraints = model
                .allowed_tuples
                .iter()
                .filter(|constraint| constraint.tuples.is_empty())
                .map(|constraint| {
                    let variable_names = constraint
                        .variables
                        .iter()
                        .map(|variable| {
                            model.variables[variable.0]
                                .debug_name
                                .clone()
                                .unwrap_or_else(|| format!("{variable:?}"))
                        })
                        .collect::<Vec<_>>();
                    (constraint, variable_names)
                })
                .collect::<Vec<_>>();
            assert!(
                empty_constraints.is_empty(),
                "source_match model has empty allowed-tuple constraints: {empty_constraints:#?}"
            );
            let projection = &model.target_projections[0];
            let binding_variable = match projection.binding_projection {
                Some(TargetBindingProjection::Variable(variable)) => variable,
                _ => panic!("alpha_all source_match should project its binding variable"),
            };

            assert_eq!(
                satisfying_tuples_for(&model, &[projection.owner_variable, binding_variable]),
                vec![vec![owner(0), string("actual")]]
            );
        });
    }

    #[test]
    fn alpha_all_binding_group_model_accepts_multideclarator_chunk_facts() {
        js_ast::with_swc_globals(|| {
            let mut group_selector = AnonymousStatementSelector::exact(
                "const primary = EXPR_PRIMARY, secondary = EXPR_SECONDARY;",
            );
            group_selector.identifiers = SourceMatchIdentifierMode::AlphaAll;
            let mut primary_selector = group_selector.clone();
            primary_selector.target_binding = Some("primary".to_string());
            let mut secondary_selector = group_selector.clone();
            secondary_selector.target_binding = Some("secondary".to_string());

            let mut builder = MemberSelectorProgramBuilder::new(
                MemberSelectorLoweringContext::new(ChunkId(0), "static/app::settings"),
            );
            builder
                .declare_member_target_in_module(
                    "static/app::settings",
                    "primary",
                    &MemberSelectorSpec::SourceMatch(primary_selector),
                )
                .unwrap();
            builder
                .declare_member_target_in_module(
                    "static/app::settings",
                    "secondary",
                    &MemberSelectorSpec::SourceMatch(secondary_selector),
                )
                .unwrap();
            assert!(
                builder
                    .try_lower_native_source_match_group(
                        "static/app::settings",
                        &group_selector,
                        &BTreeMap::from([
                            ("primary".to_string(), "primary".to_string()),
                            ("secondary".to_string(), "secondary".to_string()),
                        ]),
                    )
                    .unwrap()
            );
            let program = builder.into_program().unwrap();

            let module = js_ast::parse_js_module_ast(
                "<chunk>",
                "const primary = 10, secondary = 20;\nexport { primary, secondary };\n",
            )
            .unwrap();
            let chunk_facts = chunk_facts::extract_facts(&module).unwrap();
            let mut facts = SelectorFactStore::default();
            facts.extend_chunk_facts(ChunkId(0), &chunk_facts);
            facts.push(owner_fact(0, 0, "var_decl"));
            facts.push(declared_binding(0, "primary"));
            facts.push(declared_binding(0, "secondary"));

            let model = compile_selector_problem(&program, &facts).unwrap();
            let empty_constraints = model
                .allowed_tuples
                .iter()
                .filter(|constraint| constraint.tuples.is_empty())
                .map(|constraint| {
                    let variable_names = constraint
                        .variables
                        .iter()
                        .map(|variable| {
                            model.variables[variable.0]
                                .debug_name
                                .clone()
                                .unwrap_or_else(|| format!("{variable:?}"))
                        })
                        .collect::<Vec<_>>();
                    (constraint, variable_names)
                })
                .collect::<Vec<_>>();
            assert!(
                empty_constraints.is_empty(),
                "binding group model has empty allowed-tuple constraints: {empty_constraints:#?}"
            );
        });
    }

    #[test]
    fn ordinal_before_lowers_to_linear_constraint() {
        let mut program = SelectorProgram::default();
        let left = program.add_variable(VariableDomain::StatementOrdinal, Some("left".to_string()));
        let right =
            program.add_variable(VariableDomain::StatementOrdinal, Some("right".to_string()));
        program.add_atom(SelectorAtom::OrdinalBefore {
            before: OrdinalTerm::Var { id: left },
            after: OrdinalTerm::Var { id: right },
        });

        let facts = fact_store(vec![owner_fact(10, 0, "var"), owner_fact(20, 1, "var")]);

        let model = compile_selector_problem(&program, &facts).unwrap();

        assert!(model.allowed_tuples.is_empty());
        assert!(model.binary_constraints.is_empty());
        assert_eq!(
            model.linear_constraints,
            vec![LinearConstraint {
                variables: vec![ConstraintVariableId(0), ConstraintVariableId(1)],
                coefficients: vec![1, -1],
                offset: 1,
                domain: vec![0, 0],
            }]
        );
        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(0)),
            vec![ordinal(0), ordinal(1)]
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
            allowed_tuples_for(&model, &[ConstraintVariableId(0), ConstraintVariableId(8)]).tuples,
            vec![
                vec![owner(20), string("target")],
                vec![owner(90), string("define")],
            ]
        );
        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(1)]).tuples,
            vec![vec![owner(10)]]
        );
        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(2)]).tuples,
            vec![vec![owner(10)]]
        );
        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(3), ConstraintVariableId(9)]).tuples,
            vec![
                vec![owner(40), string("size")],
                vec![owner(40), string("value")],
            ]
        );
        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(10)]).tuples,
            vec![vec![string("value")]]
        );
        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(4)]).tuples,
            vec![vec![owner(50)]]
        );
        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(5)]).tuples,
            vec![vec![owner(60)]]
        );
        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(6)]).tuples,
            vec![vec![owner(70)]]
        );
        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(7)]).tuples,
            vec![vec![owner(90)]]
        );
        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(11)]).tuples,
            vec![vec![owner(65)]]
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
            allowed_tuples_for(&model, &[ConstraintVariableId(0)]).tuples,
            vec![vec![owner(10)]]
        );
        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(1)]).tuples,
            vec![vec![owner(20)]]
        );
        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(2)]).tuples,
            vec![vec![owner(30)]]
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
