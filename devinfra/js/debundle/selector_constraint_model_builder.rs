//! Backend-neutral lowering from selector IR plus facts into a finite-domain
//! constraint model.
//!
//! This is intentionally a shadow builder: it exposes the model boundary a
//! CP/SAT backend can consume without changing the current Ascent-backed solver
//! path.

use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

use analysis::{OwnerId, StatementOrdinal};
use chunk_facts::NodeId;
use selector_constraint_model::{
    AllDifferentReason, BinaryConstraintKind, ConstraintModelError, ConstraintValue,
    ConstraintVariableId, SelectorConstraintModel,
};
use selector_ir::{
    ClaimKind, NodeTerm, OrdinalTerm, OwnerTerm, SelectorAtom, SelectorFact, SelectorFactStore,
    SelectorProgram, SelectorProgramError, SelectorVariableId, StringTerm, VariableDomain,
};

pub fn build_selector_constraint_model(
    program: &SelectorProgram,
    facts: &SelectorFactStore,
) -> Result<SelectorConstraintModel, SelectorConstraintModelBuildError> {
    program
        .validate()
        .map_err(SelectorConstraintModelBuildError::InvalidProgram)?;

    let domains = FactDomains::from_program_and_facts(program, facts);
    let target_binding_projections = TargetBindingProjections::from_program(program)?;

    let mut model = SelectorConstraintModel::default();
    let mut variables = Vec::with_capacity(program.variables.len());
    for variable in &program.variables {
        variables.push(model.add_variable(
            variable.id,
            variable.domain,
            domains.values_for(variable.domain),
            variable.debug_name.clone(),
        )?);
    }

    for target in &program.targets {
        let owner_variable = model_variable(&variables, target.owner)?;
        let binding_projection = target_binding_projections.binding_projection(target.owner);
        let binding_variable = binding_projection
            .and_then(TargetBindingProjection::variable)
            .map(|binding| model_variable(&variables, binding))
            .transpose()?;
        let binding_const = binding_projection.and_then(TargetBindingProjection::constant);
        model.add_target_projection_with_binding_const(
            target.id,
            owner_variable,
            binding_variable,
            binding_const,
        )?;
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

    model.validate()?;
    Ok(model)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SelectorConstraintModelBuildError {
    InvalidProgram(SelectorProgramError),
    InvalidModel(ConstraintModelError),
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
        existing: TargetBindingProjection,
        actual: TargetBindingProjection,
    },
}

impl fmt::Display for SelectorConstraintModelBuildError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidProgram(err) => write!(f, "invalid selector program: {err}"),
            Self::InvalidModel(err) => write!(f, "invalid selector constraint model: {err}"),
            Self::UnknownSelectorVariable { variable } => {
                write!(f, "selector variable {variable:?} has no model variable")
            }
            Self::UnsupportedAtom { atom } => {
                write!(
                    f,
                    "selector atom is not supported by the constraint model builder: {atom}"
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

impl Error for SelectorConstraintModelBuildError {
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

impl From<ConstraintModelError> for SelectorConstraintModelBuildError {
    fn from(err: ConstraintModelError) -> Self {
        Self::InvalidModel(err)
    }
}

fn model_variable(
    variables: &[ConstraintVariableId],
    variable: SelectorVariableId,
) -> Result<ConstraintVariableId, SelectorConstraintModelBuildError> {
    variables
        .get(variable.0)
        .copied()
        .ok_or(SelectorConstraintModelBuildError::UnknownSelectorVariable { variable })
}

fn lower_atom_constraint(
    atom: &SelectorAtom,
    domains: &FactDomains,
    variables: &[ConstraintVariableId],
    model: &mut SelectorConstraintModel,
) -> Result<(), SelectorConstraintModelBuildError> {
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
        } => {
            let facts = domains
                .ast_children
                .iter()
                .filter_map(|(fact_parent, fact_index, fact_child)| {
                    (*fact_index == *index).then_some((*fact_parent, *fact_child))
                })
                .collect::<BTreeSet<_>>();
            add_node_node_allowed_tuples(model, variables, parent, child, &facts)
        }
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
            &domains.ast_top_levels,
        ),
        SelectorAtom::OrdinalOffset {
            base,
            ordinal,
            offset,
        } => add_ordinal_ordinal_allowed_tuples(
            model,
            variables,
            base,
            ordinal,
            &domains.ordinal_offset_rows(*offset),
        ),
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
        SelectorAtom::OrdinalBefore {
            before: OrdinalTerm::Var { id: before },
            after: OrdinalTerm::Var { id: after },
        } => model
            .add_binary_constraint(
                model_variable(variables, *before)?,
                model_variable(variables, *after)?,
                BinaryConstraintKind::OrdinalBefore,
            )
            .map_err(Into::into),
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
        _ => Err(SelectorConstraintModelBuildError::UnsupportedAtom {
            atom: format!("{atom:?}"),
        }),
    }
}

fn add_owner_string_allowed_tuples(
    model: &mut SelectorConstraintModel,
    variables: &[ConstraintVariableId],
    owner: &OwnerTerm,
    string: &StringTerm,
    facts: &BTreeSet<(OwnerId, String)>,
) -> Result<(), SelectorConstraintModelBuildError> {
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
                || SelectorConstraintModelBuildError::ConstantOnlyAtomUnsatisfied {
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
                tuple.push(ConstraintValue::Owner(*fact_owner));
            }
            if matches!(string, StringTerm::Var { .. }) {
                tuple.push(ConstraintValue::String(fact_string.clone()));
            }
            tuple
        })
        .collect::<BTreeSet<_>>();

    add_allowed_tuple_set(model, constraint_variables, tuples)
}

fn add_owner_ordinal_allowed_tuples(
    model: &mut SelectorConstraintModel,
    variables: &[ConstraintVariableId],
    owner: &OwnerTerm,
    ordinal: &OrdinalTerm,
    facts: &BTreeSet<(OwnerId, StatementOrdinal)>,
) -> Result<(), SelectorConstraintModelBuildError> {
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
                || SelectorConstraintModelBuildError::ConstantOnlyAtomUnsatisfied {
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
                tuple.push(ConstraintValue::Owner(*fact_owner));
            }
            if matches!(ordinal, OrdinalTerm::Var { .. }) {
                tuple.push(ConstraintValue::StatementOrdinal(*fact_ordinal));
            }
            tuple
        })
        .collect::<BTreeSet<_>>();

    add_allowed_tuple_set(model, constraint_variables, tuples)
}

fn add_owner_allowed_tuples(
    model: &mut SelectorConstraintModel,
    variables: &[ConstraintVariableId],
    owner: &OwnerTerm,
    facts: &BTreeSet<OwnerId>,
) -> Result<(), SelectorConstraintModelBuildError> {
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
                || SelectorConstraintModelBuildError::ConstantOnlyAtomUnsatisfied {
                    atom: format!("owner fact {owner:?}"),
                },
            );
    }

    let tuples = facts
        .iter()
        .filter(|fact_owner| owner_term_matches(owner, **fact_owner))
        .map(|fact_owner| vec![ConstraintValue::Owner(*fact_owner)])
        .collect::<BTreeSet<_>>();

    add_allowed_tuple_set(model, constraint_variables, tuples)
}

fn add_owner_owner_allowed_tuples(
    model: &mut SelectorConstraintModel,
    variables: &[ConstraintVariableId],
    left: &OwnerTerm,
    right: &OwnerTerm,
    facts: &BTreeSet<(OwnerId, OwnerId)>,
) -> Result<(), SelectorConstraintModelBuildError> {
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
                || SelectorConstraintModelBuildError::ConstantOnlyAtomUnsatisfied {
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
                tuple.push(ConstraintValue::Owner(*fact_left));
            }
            if matches!(right, OwnerTerm::Var { .. }) {
                tuple.push(ConstraintValue::Owner(*fact_right));
            }
            tuple
        })
        .collect::<BTreeSet<_>>();

    add_allowed_tuple_set(model, constraint_variables, tuples)
}

fn add_owner_node_allowed_tuples(
    model: &mut SelectorConstraintModel,
    variables: &[ConstraintVariableId],
    owner: &OwnerTerm,
    node: &NodeTerm,
    facts: &BTreeSet<(OwnerId, NodeId)>,
) -> Result<(), SelectorConstraintModelBuildError> {
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
                || SelectorConstraintModelBuildError::ConstantOnlyAtomUnsatisfied {
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
                tuple.push(ConstraintValue::Owner(*fact_owner));
            }
            if matches!(node, NodeTerm::Var { .. }) {
                tuple.push(ConstraintValue::AstNode(*fact_node));
            }
            tuple
        })
        .collect::<BTreeSet<_>>();

    add_allowed_tuple_set(model, constraint_variables, tuples)
}

fn add_node_node_allowed_tuples(
    model: &mut SelectorConstraintModel,
    variables: &[ConstraintVariableId],
    left: &NodeTerm,
    right: &NodeTerm,
    facts: &BTreeSet<(NodeId, NodeId)>,
) -> Result<(), SelectorConstraintModelBuildError> {
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
                || SelectorConstraintModelBuildError::ConstantOnlyAtomUnsatisfied {
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
                tuple.push(ConstraintValue::AstNode(*fact_left));
            }
            if matches!(right, NodeTerm::Var { .. }) {
                tuple.push(ConstraintValue::AstNode(*fact_right));
            }
            tuple
        })
        .collect::<BTreeSet<_>>();

    add_allowed_tuple_set(model, constraint_variables, tuples)
}

fn add_node_string_allowed_tuples(
    model: &mut SelectorConstraintModel,
    variables: &[ConstraintVariableId],
    node: &NodeTerm,
    string: &StringTerm,
    facts: &BTreeSet<(NodeId, String)>,
) -> Result<(), SelectorConstraintModelBuildError> {
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
                || SelectorConstraintModelBuildError::ConstantOnlyAtomUnsatisfied {
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
                tuple.push(ConstraintValue::AstNode(*fact_node));
            }
            if matches!(string, StringTerm::Var { .. }) {
                tuple.push(ConstraintValue::String(fact_string.clone()));
            }
            tuple
        })
        .collect::<BTreeSet<_>>();

    add_allowed_tuple_set(model, constraint_variables, tuples)
}

fn add_node_ordinal_allowed_tuples(
    model: &mut SelectorConstraintModel,
    variables: &[ConstraintVariableId],
    node: &NodeTerm,
    ordinal: &OrdinalTerm,
    facts: &BTreeSet<(NodeId, StatementOrdinal)>,
) -> Result<(), SelectorConstraintModelBuildError> {
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
                || SelectorConstraintModelBuildError::ConstantOnlyAtomUnsatisfied {
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
                tuple.push(ConstraintValue::AstNode(*fact_node));
            }
            if matches!(ordinal, OrdinalTerm::Var { .. }) {
                tuple.push(ConstraintValue::StatementOrdinal(*fact_ordinal));
            }
            tuple
        })
        .collect::<BTreeSet<_>>();

    add_allowed_tuple_set(model, constraint_variables, tuples)
}

fn add_ordinal_ordinal_allowed_tuples(
    model: &mut SelectorConstraintModel,
    variables: &[ConstraintVariableId],
    base: &OrdinalTerm,
    ordinal: &OrdinalTerm,
    facts: &BTreeSet<(StatementOrdinal, StatementOrdinal)>,
) -> Result<(), SelectorConstraintModelBuildError> {
    let mut constraint_variables = Vec::new();
    if let OrdinalTerm::Var { id } = base {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if let OrdinalTerm::Var { id } = ordinal {
        constraint_variables.push(model_variable(variables, *id)?);
    }
    if constraint_variables.is_empty() {
        return facts
            .iter()
            .any(|(fact_base, fact_ordinal)| {
                ordinal_term_matches(base, *fact_base)
                    && ordinal_term_matches(ordinal, *fact_ordinal)
            })
            .then_some(())
            .ok_or_else(
                || SelectorConstraintModelBuildError::ConstantOnlyAtomUnsatisfied {
                    atom: format!("ordinal/ordinal fact {base:?} {ordinal:?}"),
                },
            );
    }

    let tuples = facts
        .iter()
        .filter(|(fact_base, fact_ordinal)| {
            ordinal_term_matches(base, *fact_base) && ordinal_term_matches(ordinal, *fact_ordinal)
        })
        .map(|(fact_base, fact_ordinal)| {
            let mut tuple = Vec::with_capacity(constraint_variables.len());
            if matches!(base, OrdinalTerm::Var { .. }) {
                tuple.push(ConstraintValue::StatementOrdinal(*fact_base));
            }
            if matches!(ordinal, OrdinalTerm::Var { .. }) {
                tuple.push(ConstraintValue::StatementOrdinal(*fact_ordinal));
            }
            tuple
        })
        .collect::<BTreeSet<_>>();

    add_allowed_tuple_set(model, constraint_variables, tuples)
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
    ) -> Result<Self, SelectorConstraintModelBuildError> {
        let parent = lower_node_term(terms.parent, variables)?;
        let mut segments = Vec::with_capacity(terms.segments.len());
        for segment in terms.segments {
            let mut lowered_segment = Vec::with_capacity(segment.len());
            for child in segment {
                lowered_segment.push(lower_node_term(child, variables)?);
            }
            segments.push(lowered_segment);
        }
        Ok(Self {
            parent,
            start_index: terms.start_index,
            segments,
            anchored_left: terms.anchored_left,
            anchored_right: terms.anchored_right,
        })
    }

    fn variables(&self) -> Vec<ConstraintVariableId> {
        let mut variables = Vec::new();
        let mut seen = BTreeSet::new();
        for term in std::iter::once(self.parent).chain(self.segments.iter().flatten().copied()) {
            if let LoweredNodeTerm::Var(variable) = term
                && seen.insert(variable)
            {
                variables.push(variable);
            }
        }
        variables
    }
}

fn add_ast_child_list_pattern_allowed_tuples(
    model: &mut SelectorConstraintModel,
    variables: &[ConstraintVariableId],
    terms: ChildListPatternTerms<'_>,
    domains: &FactDomains,
) -> Result<(), SelectorConstraintModelBuildError> {
    let pattern = LoweredChildListPattern::from_terms(terms, variables)?;

    if pattern.segments.iter().all(Vec::is_empty) {
        return Ok(());
    }

    let constraint_variables = pattern.variables();
    let mut tuples = BTreeSet::new();
    let mut constant_only_match = false;

    for candidate_parent in child_list_candidate_parents(pattern.parent, &domains.ast_children) {
        let subject_children = child_list_subject_children(
            &domains.ast_children,
            candidate_parent,
            pattern.start_index,
        );

        let mut current = Vec::new();
        if !bind_node_term(pattern.parent, candidate_parent, &mut current) {
            continue;
        }
        ChildListTupleCollector {
            pattern: &pattern,
            subject_children: &subject_children,
            constraint_variables: &constraint_variables,
            tuples: &mut tuples,
            constant_only_match: &mut constant_only_match,
        }
        .collect(0, 0, &mut current);
    }

    if constraint_variables.is_empty() {
        return constant_only_match.then_some(()).ok_or_else(|| {
            SelectorConstraintModelBuildError::ConstantOnlyAtomUnsatisfied {
                atom: "ast_child_list_pattern".to_string(),
            }
        });
    }

    add_allowed_tuple_set(model, constraint_variables, tuples)
}

fn lower_node_term(
    term: &NodeTerm,
    variables: &[ConstraintVariableId],
) -> Result<LoweredNodeTerm, SelectorConstraintModelBuildError> {
    match term {
        NodeTerm::Var { id } => model_variable(variables, *id).map(LoweredNodeTerm::Var),
        NodeTerm::Const { node } => Ok(LoweredNodeTerm::Const(*node)),
    }
}

fn child_list_candidate_parents(
    parent: LoweredNodeTerm,
    ast_children: &BTreeSet<(NodeId, u32, NodeId)>,
) -> Box<dyn Iterator<Item = NodeId> + '_> {
    match parent {
        LoweredNodeTerm::Const(node) => Box::new(std::iter::once(node)),
        LoweredNodeTerm::Var(_) => Box::new(
            ast_children
                .iter()
                .map(|(parent, _index, _child)| *parent)
                .collect::<BTreeSet<_>>()
                .into_iter(),
        ),
    }
}

fn child_list_subject_children(
    ast_children: &BTreeSet<(NodeId, u32, NodeId)>,
    parent: NodeId,
    start_index: u32,
) -> Vec<NodeId> {
    ast_children
        .iter()
        .filter(|(fact_parent, index, _child)| *fact_parent == parent && *index >= start_index)
        .map(|(_parent, _index, child)| *child)
        .collect()
}

struct ChildListTupleCollector<'a> {
    pattern: &'a LoweredChildListPattern,
    subject_children: &'a [NodeId],
    constraint_variables: &'a [ConstraintVariableId],
    tuples: &'a mut BTreeSet<Vec<ConstraintValue>>,
    constant_only_match: &'a mut bool,
}

impl ChildListTupleCollector<'_> {
    fn collect(
        &mut self,
        segment_index: usize,
        candidate_min: usize,
        current: &mut Vec<(ConstraintVariableId, ConstraintValue)>,
    ) {
        let Some(segment) = self.pattern.segments.get(segment_index) else {
            self.finish_row(current);
            return;
        };

        let remaining: usize = self.pattern.segments[segment_index..]
            .iter()
            .map(Vec::len)
            .sum();
        let Some(latest_start) = self.subject_children.len().checked_sub(remaining) else {
            return;
        };
        let mut lo = candidate_min;
        let mut hi = latest_start;
        if segment_index == 0 && self.pattern.anchored_left {
            hi = hi.min(0);
        }
        if segment_index == self.pattern.segments.len() - 1 && self.pattern.anchored_right {
            lo = lo.max(latest_start);
        }
        if lo > hi {
            return;
        }

        for start in lo..=hi {
            let current_len = current.len();
            let mut segment_matches = true;
            for (offset, term) in segment.iter().enumerate() {
                if !bind_node_term(*term, self.subject_children[start + offset], current) {
                    segment_matches = false;
                    break;
                }
            }
            if segment_matches {
                self.collect(segment_index + 1, start + segment.len(), current);
            }
            current.truncate(current_len);
        }
    }

    fn finish_row(&mut self, current: &[(ConstraintVariableId, ConstraintValue)]) {
        let Some(merged) = merge_node_assignment_bindings(current.to_vec()) else {
            return;
        };
        if self.constraint_variables.is_empty() {
            *self.constant_only_match = true;
            return;
        }
        let assignments = merged.into_iter().collect::<BTreeMap<_, _>>();
        if let Some(tuple) = self
            .constraint_variables
            .iter()
            .map(|variable| assignments.get(variable).cloned())
            .collect::<Option<Vec<_>>>()
        {
            self.tuples.insert(tuple);
        }
    }
}

fn bind_node_term(
    term: LoweredNodeTerm,
    actual: NodeId,
    current: &mut Vec<(ConstraintVariableId, ConstraintValue)>,
) -> bool {
    match term {
        LoweredNodeTerm::Var(variable) => {
            current.push((variable, ConstraintValue::AstNode(actual)));
            true
        }
        LoweredNodeTerm::Const(expected) => expected == actual,
    }
}

fn merge_node_assignment_bindings(
    mut row: Vec<(ConstraintVariableId, ConstraintValue)>,
) -> Option<Vec<(ConstraintVariableId, ConstraintValue)>> {
    row.sort_by_key(|(variable, _value)| *variable);
    let mut merged = Vec::with_capacity(row.len());
    for (variable, value) in row {
        if let Some((last_variable, last_value)) = merged.last() {
            if *last_variable == variable {
                if *last_value != value {
                    return None;
                }
                continue;
            }
        }
        merged.push((variable, value));
    }
    Some(merged)
}

fn add_ast_bare_property_allowed_tuples(
    model: &mut SelectorConstraintModel,
    variables: &[ConstraintVariableId],
    node: &NodeTerm,
    key: &StringTerm,
    identifier: &StringTerm,
    is_binding: bool,
    facts: &BTreeSet<(NodeId, String, String, bool)>,
) -> Result<(), SelectorConstraintModelBuildError> {
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
                || SelectorConstraintModelBuildError::ConstantOnlyAtomUnsatisfied {
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
                tuple.push(ConstraintValue::AstNode(*fact_node));
            }
            if matches!(key, StringTerm::Var { .. }) {
                tuple.push(ConstraintValue::String(fact_key.clone()));
            }
            if matches!(identifier, StringTerm::Var { .. }) {
                tuple.push(ConstraintValue::String(fact_identifier.clone()));
            }
            tuple
        })
        .collect::<BTreeSet<_>>();

    add_allowed_tuple_set(model, constraint_variables, tuples)
}

fn add_ast_regex_literal_allowed_tuples(
    model: &mut SelectorConstraintModel,
    variables: &[ConstraintVariableId],
    node: &NodeTerm,
    pattern: &StringTerm,
    flags: &StringTerm,
    facts: &BTreeSet<(NodeId, String, String)>,
) -> Result<(), SelectorConstraintModelBuildError> {
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
                || SelectorConstraintModelBuildError::ConstantOnlyAtomUnsatisfied {
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
                tuple.push(ConstraintValue::AstNode(*fact_node));
            }
            if matches!(pattern, StringTerm::Var { .. }) {
                tuple.push(ConstraintValue::String(fact_pattern.clone()));
            }
            if matches!(flags, StringTerm::Var { .. }) {
                tuple.push(ConstraintValue::String(fact_flags.clone()));
            }
            tuple
        })
        .collect::<BTreeSet<_>>();

    add_allowed_tuple_set(model, constraint_variables, tuples)
}

fn add_allowed_tuple_set(
    model: &mut SelectorConstraintModel,
    variables: Vec<ConstraintVariableId>,
    tuples: BTreeSet<Vec<ConstraintValue>>,
) -> Result<(), SelectorConstraintModelBuildError> {
    model
        .add_allowed_tuples(variables, tuples.into_iter().collect())
        .map(|_| ())
        .map_err(Into::into)
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

fn optional_string_term_const(
    term: &Option<StringTerm>,
) -> Result<Option<String>, SelectorConstraintModelBuildError> {
    match term {
        Some(StringTerm::Const { value }) => Ok(Some(value.clone())),
        Some(StringTerm::Var { .. }) => Err(SelectorConstraintModelBuildError::UnsupportedAtom {
            atom: "selector relation currently requires a constant optional string".to_string(),
        }),
        None => Ok(None),
    }
}

fn required_string_term_const(
    term: &StringTerm,
    context: &'static str,
) -> Result<String, SelectorConstraintModelBuildError> {
    match term {
        StringTerm::Const { value } => Ok(value.clone()),
        StringTerm::Var { .. } => Err(SelectorConstraintModelBuildError::UnsupportedAtom {
            atom: format!("{context} currently requires a constant string"),
        }),
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TargetBindingProjection {
    Const(String),
    Var(SelectorVariableId),
}

#[derive(Debug, Default)]
struct TargetBindingProjections {
    by_owner: BTreeMap<SelectorVariableId, TargetBindingProjection>,
}

impl TargetBindingProjections {
    fn from_program(program: &SelectorProgram) -> Result<Self, SelectorConstraintModelBuildError> {
        let mut projections = Self::default();
        for atom in &program.atoms {
            match atom {
                SelectorAtom::OwnerDeclaresBinding {
                    owner: OwnerTerm::Var { id: owner },
                    binding: StringTerm::Const { value },
                } => projections.insert(*owner, TargetBindingProjection::Const(value.clone()))?,
                SelectorAtom::OwnerDeclaresBinding {
                    owner: OwnerTerm::Var { id: owner },
                    binding: StringTerm::Var { id: binding },
                } => projections.insert(*owner, TargetBindingProjection::Var(*binding))?,
                _ => {}
            }
        }
        Ok(projections)
    }

    fn insert(
        &mut self,
        owner: SelectorVariableId,
        binding: TargetBindingProjection,
    ) -> Result<(), SelectorConstraintModelBuildError> {
        match self.by_owner.get(&owner) {
            Some(existing) if existing != &binding => Err(
                SelectorConstraintModelBuildError::ConflictingTargetBindingProjection {
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

    fn binding_projection(&self, owner: SelectorVariableId) -> Option<&TargetBindingProjection> {
        self.by_owner.get(&owner)
    }
}

impl TargetBindingProjection {
    fn variable(&self) -> Option<SelectorVariableId> {
        match self {
            Self::Var(binding) => Some(*binding),
            Self::Const(_) => None,
        }
    }

    fn constant(&self) -> Option<String> {
        match self {
            Self::Const(binding) => Some(binding.clone()),
            Self::Var(_) => None,
        }
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
    ast_children: BTreeSet<(NodeId, u32, NodeId)>,
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
    raw_member_reads: BTreeSet<(StatementOrdinal, Option<String>, String)>,
    member_reads: BTreeSet<(OwnerId, String)>,
    member_reads_from_binding: BTreeSet<(OwnerId, String, String)>,
    reads_member_of_owner: BTreeSet<(OwnerId, OwnerId, String)>,
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
        domains.add_derived_facts();
        domains.add_program_constants(program);
        domains
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
                    self.ast_children.insert((*parent, *index, *child));
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
                }
                SelectorFact::CallArgumentUse {
                    argument,
                    callee_object,
                    callee_member,
                    ..
                } => {
                    self.add_string(argument);
                    if let Some(callee_object) = callee_object {
                        self.add_string(callee_object);
                    }
                    self.add_string(callee_member);
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
            for (referencer, used_binding, _edge_kind) in &self.raw_owner_references_binding {
                if used_binding != binding {
                    continue;
                }
                self.intrinsic_alias_referenced_by.extend(
                    alias_owners
                        .iter()
                        .map(|alias_owner| (*alias_owner, property.clone(), *referencer)),
                );
            }
        }

        let mut child_counts = BTreeMap::new();
        for (parent, index, _child) in &self.ast_children {
            let count = child_counts.entry(*parent).or_insert(0);
            *count = (*count).max(index + 1);
        }
        self.ast_child_counts
            .extend(child_counts.into_iter().collect::<BTreeSet<_>>());

        for (owner, ordinal) in &self.owner_statement_ordinals {
            for (node, top_level_ordinal) in &self.ast_top_levels {
                if ordinal == top_level_ordinal {
                    self.owner_top_level_roots.insert((*owner, *node));
                }
            }
        }
    }

    fn ordinal_offset_rows(&self, offset: i32) -> BTreeSet<(StatementOrdinal, StatementOrdinal)> {
        let ordinals = self
            .ast_top_levels
            .iter()
            .map(|(_node, ordinal)| *ordinal)
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect::<Vec<_>>();
        let mut rows = BTreeSet::new();
        for (base_index, base) in ordinals.iter().enumerate() {
            let Some(target_index) = (base_index as isize).checked_add(offset as isize) else {
                continue;
            };
            if target_index < 0 {
                continue;
            }
            let Some(ordinal) = ordinals.get(target_index as usize) else {
                continue;
            };
            rows.insert((*base, *ordinal));
        }
        rows
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

#[cfg(test)]
mod tests {
    use super::*;
    use analysis::{ChunkId, OwnerId, StatementOrdinal};
    use chunk_facts::NodeKind;
    use selector_constraint_model::{
        AllDifferentConstraint, AllDifferentConstraintId, AllDifferentReason,
        AllowedTupleConstraint, AllowedTupleConstraintId, BinaryConstraint, BinaryConstraintKind,
        ConstraintValue,
    };
    use selector_ir::{ClaimOrigin, SelectorTargetId};

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

    fn allowed_tuples_for<'a>(
        model: &'a SelectorConstraintModel,
        variables: &[ConstraintVariableId],
    ) -> &'a AllowedTupleConstraint {
        model
            .allowed_tuples
            .iter()
            .find(|constraint| constraint.variables == variables)
            .unwrap()
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

        let model = build_selector_constraint_model(&program, &facts).unwrap();

        assert_eq!(model.target_projections.len(), 2);
        assert_eq!(model.target_projections[0].target, broad_target);
        assert_eq!(
            model.target_projections[0].owner_variable,
            ConstraintVariableId(0)
        );
        assert_eq!(model.target_projections[0].binding_variable, None);
        assert_eq!(
            model.target_projections[0].binding_const.as_deref(),
            Some("shared")
        );
        assert_eq!(model.target_projections[1].target, strict_target);
        assert_eq!(
            model.target_projections[1].owner_variable,
            ConstraintVariableId(1)
        );
        assert_eq!(model.target_projections[1].binding_variable, None);
        assert_eq!(
            model.target_projections[1].binding_const.as_deref(),
            Some("specific")
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

        let model = build_selector_constraint_model(&program, &facts).unwrap();

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

        let model = build_selector_constraint_model(&program, &facts).unwrap();

        assert_eq!(model.target_projections.len(), 1);
        assert_eq!(model.target_projections[0].target, target);
        assert_eq!(
            model.target_projections[0].owner_variable,
            ConstraintVariableId(0)
        );
        assert_eq!(
            model.target_projections[0].binding_variable,
            Some(ConstraintVariableId(1))
        );
        assert_eq!(model.target_projections[0].binding_const, None);
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

        let model = build_selector_constraint_model(&program, &facts).unwrap();

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
            allowed_tuples_for(&model, &[ConstraintVariableId(6), ConstraintVariableId(7)]).tuples,
            vec![vec![ordinal(0), ordinal(1)]]
        );
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

        let model = build_selector_constraint_model(&program, &facts).unwrap();

        assert_eq!(
            allowed_tuples_for(
                &model,
                &[
                    ConstraintVariableId(0),
                    ConstraintVariableId(1),
                    ConstraintVariableId(2)
                ]
            ),
            &AllowedTupleConstraint {
                id: AllowedTupleConstraintId(0),
                variables: vec![
                    ConstraintVariableId(0),
                    ConstraintVariableId(1),
                    ConstraintVariableId(2)
                ],
                tuples: vec![
                    vec![ast_node(100), ast_node(10), ast_node(20)],
                    vec![ast_node(100), ast_node(10), ast_node(30)],
                    vec![ast_node(100), ast_node(20), ast_node(30)],
                    vec![ast_node(200), ast_node(20), ast_node(10)],
                ],
            }
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

        let model = build_selector_constraint_model(&program, &facts).unwrap();

        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(0)]),
            &AllowedTupleConstraint {
                id: AllowedTupleConstraintId(0),
                variables: vec![ConstraintVariableId(0)],
                tuples: vec![vec![ast_node(100)]],
            }
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

        let model = build_selector_constraint_model(&program, &facts).unwrap();

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

        let model = build_selector_constraint_model(&program, &facts).unwrap();

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
        let mut model = SelectorConstraintModel::default();
        let parent_model_var = model
            .add_variable(
                parent,
                VariableDomain::AstNode,
                vec![ast_node(100), ast_node(200)],
                Some("parent".to_string()),
            )
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

        assert_eq!(model.allowed_tuples, Vec::<AllowedTupleConstraint>::new());
        assert_eq!(
            model.variables[0].values,
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

        let model = build_selector_constraint_model(&program, &facts).unwrap();

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
    fn explicit_binary_atoms_stay_binary_constraints() {
        let mut program = SelectorProgram::default();
        let left = program.add_variable(VariableDomain::StatementOrdinal, Some("left".to_string()));
        let right =
            program.add_variable(VariableDomain::StatementOrdinal, Some("right".to_string()));
        program.add_atom(SelectorAtom::OrdinalBefore {
            before: OrdinalTerm::Var { id: left },
            after: OrdinalTerm::Var { id: right },
        });

        let facts = fact_store(vec![owner_fact(10, 0, "var"), owner_fact(20, 1, "var")]);

        let model = build_selector_constraint_model(&program, &facts).unwrap();

        assert_eq!(model.allowed_tuples, Vec::<AllowedTupleConstraint>::new());
        assert_eq!(
            model.binary_constraints,
            vec![BinaryConstraint {
                left: ConstraintVariableId(0),
                right: ConstraintVariableId(1),
                kind: BinaryConstraintKind::OrdinalBefore,
            }]
        );
        assert_eq!(model.variables[0].values, vec![ordinal(0), ordinal(1)]);
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

        let model = build_selector_constraint_model(&program, &facts).unwrap();

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
    }

    #[test]
    fn unsupported_atoms_fail_closed() {
        let mut program = SelectorProgram::default();
        let owner_var = program.add_variable(VariableDomain::Owner, Some("owner".to_string()));
        program.add_atom(SelectorAtom::ConsumesModuleMember {
            owner: OwnerTerm::Var { id: owner_var },
            module: StringTerm::Const {
                value: "mod".to_string(),
            },
            member: StringTerm::Const {
                value: "value".to_string(),
            },
        });

        let facts = fact_store(vec![owner_fact(10, 0, "var")]);

        let err = build_selector_constraint_model(&program, &facts).unwrap_err();
        assert!(matches!(
            err,
            SelectorConstraintModelBuildError::UnsupportedAtom { .. }
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

        let err = build_selector_constraint_model(&program, &facts).unwrap_err();
        assert!(matches!(
            err,
            SelectorConstraintModelBuildError::ConstantOnlyAtomUnsatisfied { .. }
        ));
    }
}
