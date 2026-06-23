//! Ascent-backed solver for the global selector IR.
//!
//! The solver runs one Ascent derivation over the lowered selector constraints
//! plus chunk-level owner/fact-store relations, then projects target claims from
//! the derived candidate sets. It supports binding/name/kind constraints and
//! `source_match` candidate constraints, and the current relational selector
//! primitives. Unsupported atoms remain explicit `ClaimOutcome::Unsupported` rows
//! instead of being ignored or approximated.

use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

use analysis::{OwnerId, StatementOrdinal};
use ascent::ascent;
use selector_ir::{
    ClaimKind, ClaimOutcome, NodeTerm, OrdinalTerm, OwnerTerm, ResolvedClaim, SelectorAtom,
    SelectorFact, SelectorFactStore, SelectorProgram, SelectorProgramError, SelectorTarget,
    SelectorTargetId, SelectorVariableId, SolverClaim, SolverResult, StringTerm, VariableDomain,
};

fn optional_u32_matches(expected: &Option<u32>, actual: u32) -> bool {
    expected.is_none_or(|expected| expected == actual)
}

fn optional_string_matches(expected: &Option<String>, actual: &Option<String>) -> bool {
    match expected {
        Some(expected) => actual.as_deref() == Some(expected.as_str()),
        None => true,
    }
}

fn optional_edge_kind_matches(expected: &Option<String>, actual: &str) -> bool {
    expected
        .as_deref()
        .is_none_or(|expected| expected == actual)
}

ascent! {
    relation owner_fact(usize, usize, String); // owner, statement ordinal, statement kind
    relation declares(usize, String); // owner declares binding
    relation declared_export(usize, String); // owner exported under readable name
    relation uses(usize, String, String); // owner references binding, edge_kind
    relation member_read(usize, String); // owner reads member `.X`
    relation member_read_from(usize, String, String); // owner reads `obj.X`
    relation module_member_use(usize, String, String); // owner consumes `<module>.X`
    relation call_arg(String, String, u32); // argument binding, callee member, arg index
    relation call_arg_from(String, String, String, u32); // argument, callee object, member, index
    relation decorate_call(String, String, Option<String>); // callee, class binding, member
    relation intrinsic_alias(String, String); // binding, Object property
    relation source_match_candidate(String, usize, String); // selector key, owner, matched binding
    relation ast_kind(u32, String); // AST node, node kind tag
    relation ast_child(u32, u32, u32); // parent, child index, child
    relation ast_child_count(u32, u32); // node, number of direct AST children
    relation ast_string_literal(u32, String); // node, value
    relation ast_string_wildcard(u32, String); // needle node, wildcard token
    relation ast_number_literal(u32, String); // node, value
    relation ast_bool_literal(u32, bool); // node, value
    relation ast_identifier_name(u32, String); // node, value
    relation ast_property_name(u32, String); // node, value
    relation ast_operator(u32, String); // node, operator
    relation ast_regex_literal(u32, String, String); // node, pattern, flags
    relation ast_super_class(u32, u32); // class node, super-class expression node
    relation ast_top_level(u32, usize); // root node, statement ordinal

    relation owner_statement_ordinal(usize, usize);
    owner_statement_ordinal(*owner, *ordinal) <--
        owner_fact(owner, ordinal, _kind);

    relation owner_top_level_root(usize, u32);
    owner_top_level_root(*owner, *node) <--
        owner_fact(owner, ordinal, _kind),
        ast_top_level(node, ordinal);

    relation name_owner(String, usize);
    name_owner(binding.clone(), *owner) <-- declares(owner, binding);

    relation references_owner(usize, usize);
    references_owner(*owner, *referenced) <--
        uses(owner, binding, _edge_kind),
        declares(referenced, binding),
        declares(owner, _declared);

    relation aliases_owner(usize, usize);
    aliases_owner(*owner, *aliased) <--
        owner_fact(owner, _ordinal, statement_kind),
        uses(owner, binding, edge_kind),
        declares(aliased, binding),
        declares(owner, _declared),
        if statement_kind.as_str() == "var_decl",
        if edge_kind.as_str() == "eager_use";

    relation reads_member_edge(usize, String);
    reads_member_edge(*owner, member.clone()) <--
        member_read(owner, member),
        declares(owner, _declared);

    relation reads_member_from_owner_edge(usize, usize, String);
    reads_member_from_owner_edge(*owner, *object_owner, member.clone()) <--
        member_read_from(owner, object_binding, member),
        declares(object_owner, object_binding),
        declares(owner, _declared);

    relation consumes_module_member_edge(usize, String, String);
    consumes_module_member_edge(*owner, module.clone(), member.clone()) <--
        module_member_use(owner, module, member),
        declares(owner, _declared);

    relation passed_to_call_edge(usize, String, u32);
    passed_to_call_edge(*owner, member.clone(), *index) <--
        call_arg(argument, member, index),
        name_owner(argument, owner),
        declares(owner, _declared);

    relation passed_to_call_from_owner_edge(usize, usize, String, u32);
    passed_to_call_from_owner_edge(*owner, *object_owner, member.clone(), *index) <--
        call_arg_from(argument, object_binding, member, index),
        name_owner(argument, owner),
        declares(object_owner, object_binding),
        declares(owner, _declared);

    relation makes_decorate_call_for_owner_edge(usize, usize, Option<String>);
    makes_decorate_call_for_owner_edge(*owner, *class_owner, member.clone()) <--
        decorate_call(callee, class_binding, member),
        name_owner(callee, owner),
        declares(class_owner, class_binding),
        declares(owner, _declared);

    relation intrinsic_alias_referenced_by_edge(usize, String, usize);
    intrinsic_alias_referenced_by_edge(*alias_owner, property.clone(), *referencer) <--
        intrinsic_alias(binding, property),
        name_owner(binding, alias_owner),
        uses(referencer, binding, _edge_kind),
        declares(alias_owner, _declared);

    relation required_binding(usize, usize, String); // constraint id, variable id, binding
    relation required_export(usize, usize, String); // constraint id, variable id, export name
    relation required_kind(usize, usize, String); // constraint id, variable id, statement kind
    relation required_references_binding(usize, usize, String, Option<String>);
    relation required_reads_member(usize, usize, String);
    relation required_reads_member_from_binding(usize, usize, String, String);
    relation required_consumes_module_member(usize, usize, String, String);
    relation required_passed_to_call(usize, usize, String, Option<u32>);
    relation required_passed_to_call_from_binding(usize, usize, String, String, Option<u32>);
    relation required_makes_decorate_call_for_binding(usize, usize, String, Option<String>);
    relation required_source_match_candidate(usize, usize, String);
    relation required_owner_statement_ordinal(usize, usize, usize);

    relation constraint_support(usize, usize, usize); // constraint id, variable id, owner
    constraint_support(*constraint, *var, *owner) <--
        required_binding(constraint, var, binding),
        declares(owner, binding),
        owner_fact(owner, _ordinal, _kind);
    constraint_support(*constraint, *var, *owner) <--
        required_export(constraint, var, export_name),
        declared_export(owner, export_name),
        owner_fact(owner, _ordinal, _kind);
    constraint_support(*constraint, *var, *owner) <--
        required_kind(constraint, var, statement_kind),
        owner_fact(owner, _ordinal, actual),
        if actual == statement_kind;
    constraint_support(*constraint, *var, *owner) <--
        required_references_binding(constraint, var, binding, edge_kind),
        uses(owner, binding, actual_edge_kind),
        declares(owner, _declared),
        if optional_edge_kind_matches(edge_kind, actual_edge_kind);
    constraint_support(*constraint, *var, *owner) <--
        required_reads_member(constraint, var, member),
        reads_member_edge(owner, actual_member),
        if actual_member == member;
    constraint_support(*constraint, *var, *owner) <--
        required_reads_member_from_binding(constraint, var, object, member),
        member_read_from(owner, actual_object, actual_member),
        declares(owner, _declared),
        if actual_object == object,
        if actual_member == member;
    constraint_support(*constraint, *var, *owner) <--
        required_consumes_module_member(constraint, var, module, member),
        consumes_module_member_edge(owner, actual_module, actual_member),
        if actual_module == module,
        if actual_member == member;
    constraint_support(*constraint, *var, *owner) <--
        required_passed_to_call(constraint, var, member, arg_index),
        passed_to_call_edge(owner, actual_member, actual_index),
        if actual_member == member,
        if optional_u32_matches(arg_index, *actual_index);
    constraint_support(*constraint, *var, *owner) <--
        required_passed_to_call_from_binding(constraint, var, object, member, arg_index),
        call_arg_from(argument, actual_object, actual_member, actual_index),
        name_owner(argument, owner),
        declares(owner, _declared),
        if actual_object == object,
        if actual_member == member,
        if optional_u32_matches(arg_index, *actual_index);
    constraint_support(*constraint, *var, *owner) <--
        required_makes_decorate_call_for_binding(constraint, var, class_anchor, member),
        decorate_call(callee, actual_class_anchor, actual_member),
        name_owner(callee, owner),
        declares(owner, _declared),
        if actual_class_anchor == class_anchor,
        if optional_string_matches(member, actual_member);
    constraint_support(*constraint, *var, *owner) <--
        required_source_match_candidate(constraint, var, selector_key),
        source_match_candidate(selector_key, owner, _binding),
        owner_fact(owner, _ordinal, _kind);
    constraint_support(*constraint, *var, *owner) <--
        required_owner_statement_ordinal(constraint, var, ordinal),
        owner_statement_ordinal(owner, actual_ordinal),
        if actual_ordinal == ordinal;

    relation required_statement_ordinal_for_owner(usize, usize, usize);
    relation required_ast_kind(usize, usize, String);
    relation required_ast_child_count(usize, usize, u32);
    relation required_ast_child_parent(usize, usize, u32, u32);
    relation required_ast_child_child(usize, usize, u32, u32);
    relation required_ast_string_literal(usize, usize, String);
    relation required_ast_number_literal(usize, usize, String);
    relation required_ast_bool_literal(usize, usize, bool);
    relation required_ast_identifier_name(usize, usize, String);
    relation required_ast_property_name(usize, usize, String);
    relation required_ast_operator(usize, usize, String);
    relation required_ast_regex_literal(usize, usize, String, String);
    relation required_ast_top_level_ordinal(usize, usize, usize);
    relation required_ast_top_level_node(usize, usize, u32);

    relation node_constraint_support(usize, usize, u32); // constraint id, variable id, AST node
    node_constraint_support(*constraint, *var, *node) <--
        required_ast_kind(constraint, var, node_kind),
        ast_kind(node, actual),
        if actual == node_kind;
    node_constraint_support(*constraint, *var, *node) <--
        required_ast_child_count(constraint, var, count),
        ast_child_count(node, actual),
        if actual == count;
    node_constraint_support(*constraint, *var, *parent) <--
        required_ast_child_parent(constraint, var, index, child),
        ast_child(parent, actual_index, actual_child),
        if actual_index == index,
        if actual_child == child;
    node_constraint_support(*constraint, *var, *child) <--
        required_ast_child_child(constraint, var, parent, index),
        ast_child(actual_parent, actual_index, child),
        if actual_parent == parent,
        if actual_index == index;
    node_constraint_support(*constraint, *var, *node) <--
        required_ast_string_literal(constraint, var, value),
        ast_string_literal(node, actual),
        if actual == value;
    node_constraint_support(*constraint, *var, *node) <--
        required_ast_number_literal(constraint, var, value),
        ast_number_literal(node, actual),
        if actual == value;
    node_constraint_support(*constraint, *var, *node) <--
        required_ast_bool_literal(constraint, var, value),
        ast_bool_literal(node, actual),
        if actual == value;
    node_constraint_support(*constraint, *var, *node) <--
        required_ast_identifier_name(constraint, var, value),
        ast_identifier_name(node, actual),
        if actual == value;
    node_constraint_support(*constraint, *var, *node) <--
        required_ast_property_name(constraint, var, value),
        ast_property_name(node, actual),
        if actual == value;
    node_constraint_support(*constraint, *var, *node) <--
        required_ast_operator(constraint, var, value),
        ast_operator(node, actual),
        if actual == value;
    node_constraint_support(*constraint, *var, *node) <--
        required_ast_regex_literal(constraint, var, pattern, flags),
        ast_regex_literal(node, actual_pattern, actual_flags),
        if actual_pattern == pattern,
        if actual_flags == flags;
    node_constraint_support(*constraint, *var, *node) <--
        required_ast_top_level_ordinal(constraint, var, ordinal),
        ast_top_level(node, actual_ordinal),
        if actual_ordinal == ordinal;

    relation ordinal_constraint_support(usize, usize, usize); // constraint id, variable id, ordinal
    ordinal_constraint_support(*constraint, *var, *ordinal) <--
        required_statement_ordinal_for_owner(constraint, var, owner),
        owner_statement_ordinal(actual_owner, ordinal),
        if actual_owner == owner;
    ordinal_constraint_support(*constraint, *var, *ordinal) <--
        required_ast_top_level_node(constraint, var, node),
        ast_top_level(actual_node, ordinal),
        if actual_node == node;

    relation required_references_owner(usize, usize, usize);
    relation required_aliases_owner(usize, usize, usize);
    relation required_reads_member_of_owner(usize, usize, usize, String);
    relation required_passed_to_call_of_owner(usize, usize, usize, String, Option<u32>);
    relation required_makes_decorate_call_for_owner(usize, usize, usize, Option<String>);
    relation required_intrinsic_alias(usize, usize, usize, String);
    relation required_owner_statement_ordinal_var(usize, usize, usize);
    relation required_ast_child(usize, usize, usize, u32);
    relation required_ast_top_level(usize, usize, usize);
    relation required_owner_top_level_root(usize, usize, usize);
    relation required_ast_string_literal_var(usize, usize, usize);

    relation binary_constraint_edge(usize, usize, usize, usize, usize);
    binary_constraint_edge(*constraint, *left_var, *left_owner, *right_var, *right_owner) <--
        required_references_owner(constraint, left_var, right_var),
        references_owner(left_owner, right_owner);
    binary_constraint_edge(*constraint, *left_var, *left_owner, *right_var, *right_owner) <--
        required_aliases_owner(constraint, left_var, right_var),
        aliases_owner(left_owner, right_owner);
    binary_constraint_edge(*constraint, *left_var, *left_owner, *right_var, *right_owner) <--
        required_reads_member_of_owner(constraint, left_var, right_var, member),
        reads_member_from_owner_edge(left_owner, right_owner, actual_member),
        if actual_member == member;
    binary_constraint_edge(*constraint, *left_var, *left_owner, *right_var, *right_owner) <--
        required_passed_to_call_of_owner(constraint, left_var, right_var, member, arg_index),
        passed_to_call_from_owner_edge(left_owner, right_owner, actual_member, actual_index),
        if actual_member == member,
        if optional_u32_matches(arg_index, *actual_index);
    binary_constraint_edge(*constraint, *left_var, *left_owner, *right_var, *right_owner) <--
        required_makes_decorate_call_for_owner(constraint, left_var, right_var, member),
        makes_decorate_call_for_owner_edge(left_owner, right_owner, actual_member),
        if optional_string_matches(member, actual_member);
    binary_constraint_edge(*constraint, *left_var, *left_owner, *right_var, *right_owner) <--
        required_intrinsic_alias(constraint, left_var, right_var, property),
        intrinsic_alias_referenced_by_edge(left_owner, actual_property, right_owner),
        if actual_property == property;

    relation owner_ordinal_constraint_edge(usize, usize, usize, usize, usize);
    owner_ordinal_constraint_edge(*constraint, *owner_var, *owner, *ordinal_var, *ordinal) <--
        required_owner_statement_ordinal_var(constraint, owner_var, ordinal_var),
        owner_statement_ordinal(owner, ordinal);

    relation node_node_constraint_edge(usize, usize, u32, usize, u32);
    node_node_constraint_edge(*constraint, *parent_var, *parent, *child_var, *child) <--
        required_ast_child(constraint, parent_var, child_var, index),
        ast_child(parent, actual_index, child),
        if actual_index == index;

    relation node_ordinal_constraint_edge(usize, usize, u32, usize, usize);
    node_ordinal_constraint_edge(*constraint, *node_var, *node, *ordinal_var, *ordinal) <--
        required_ast_top_level(constraint, node_var, ordinal_var),
        ast_top_level(node, ordinal);

    relation node_string_constraint_edge(usize, usize, u32, usize, String);
    node_string_constraint_edge(*constraint, *node_var, *node, *string_var, value.clone()) <--
        required_ast_string_literal_var(constraint, node_var, string_var),
        ast_string_literal(node, value);

    relation owner_node_constraint_edge(usize, usize, usize, usize, u32);
    owner_node_constraint_edge(*constraint, *owner_var, *owner, *node_var, *node) <--
        required_owner_top_level_root(constraint, owner_var, node_var),
        owner_top_level_root(owner, node);
}

/// Solve the supported selector IR fragment against a selector fact store.
pub fn solve(
    program: &SelectorProgram,
    facts: &SelectorFactStore,
) -> Result<SolverResult, SelectorIrSolverError> {
    program
        .validate()
        .map_err(SelectorIrSolverError::InvalidProgram)?;

    let support = ProgramSupport::from_program(program);
    if !support.unsupported_atoms.is_empty() {
        return Ok(unsupported_result(
            program,
            support.unsupported_atoms.join("; "),
        ));
    }

    let fact_index = FactIndex::from_store(facts);
    let mut ascent = AscentProgram::default();
    for (owner, ordinal, kind) in &fact_index.owner_facts {
        ascent
            .owner_fact
            .push((owner.0, ordinal.0, kind.to_string()));
    }
    for (owner, binding) in &fact_index.declared_bindings {
        ascent.declares.push((owner.0, binding.clone()));
    }
    for (owner, export_name) in &fact_index.declared_exports {
        ascent.declared_export.push((owner.0, export_name.clone()));
    }
    for (owner, binding, edge_kind) in &fact_index.owner_references_binding {
        ascent
            .uses
            .push((owner.0, binding.clone(), edge_kind.clone()));
    }
    for (owner, member) in &fact_index.member_reads {
        ascent.member_read.push((owner.0, member.clone()));
    }
    for (owner, object, member) in &fact_index.member_reads_from {
        ascent
            .member_read_from
            .push((owner.0, object.clone(), member.clone()));
    }
    for (owner, module, member) in &fact_index.module_member_uses {
        ascent
            .module_member_use
            .push((owner.0, module.clone(), member.clone()));
    }
    for (argument, callee_member, arg_index) in &fact_index.call_arguments {
        ascent
            .call_arg
            .push((argument.clone(), callee_member.clone(), *arg_index as u32));
    }
    for (argument, callee_object, callee_member, arg_index) in &fact_index.call_arguments_from {
        ascent.call_arg_from.push((
            argument.clone(),
            callee_object.clone(),
            callee_member.clone(),
            *arg_index as u32,
        ));
    }
    for (callee, class_anchor, member) in &fact_index.decorate_calls {
        ascent
            .decorate_call
            .push((callee.clone(), class_anchor.clone(), member.clone()));
    }
    for (binding, property) in &fact_index.intrinsic_aliases {
        ascent
            .intrinsic_alias
            .push((binding.clone(), property.clone()));
    }
    for (selector_key, owner, binding) in &fact_index.source_match_candidates {
        ascent
            .source_match_candidate
            .push((selector_key.clone(), owner.0, binding.clone()));
    }
    for (node, node_kind) in &fact_index.ast_kinds {
        ascent.ast_kind.push((*node, node_kind.clone()));
    }
    for (parent, index, child) in &fact_index.ast_children {
        ascent.ast_child.push((*parent, *index, *child));
    }
    for (node, count) in &fact_index.ast_child_counts {
        ascent.ast_child_count.push((*node, *count));
    }
    for (node, value) in &fact_index.ast_string_literals {
        ascent.ast_string_literal.push((*node, value.clone()));
    }
    for (node, value) in &fact_index.ast_string_wildcards {
        ascent.ast_string_wildcard.push((*node, value.clone()));
    }
    for (node, value) in &fact_index.ast_number_literals {
        ascent.ast_number_literal.push((*node, value.clone()));
    }
    for (node, value) in &fact_index.ast_bool_literals {
        ascent.ast_bool_literal.push((*node, *value));
    }
    for (node, value) in &fact_index.ast_identifier_names {
        ascent.ast_identifier_name.push((*node, value.clone()));
    }
    for (node, value) in &fact_index.ast_property_names {
        ascent.ast_property_name.push((*node, value.clone()));
    }
    for (node, value) in &fact_index.ast_operators {
        ascent.ast_operator.push((*node, value.clone()));
    }
    for (node, pattern, flags) in &fact_index.ast_regex_literals {
        ascent
            .ast_regex_literal
            .push((*node, pattern.clone(), flags.clone()));
    }
    for (class_node, super_class) in &fact_index.ast_super_classes {
        ascent.ast_super_class.push((*class_node, *super_class));
    }
    for (node, ordinal) in &fact_index.ast_top_levels {
        ascent.ast_top_level.push((*node, ordinal.0));
    }
    for constraint in &support.unary_constraints {
        match &constraint.kind {
            UnaryConstraintKind::Binding { binding } => ascent.required_binding.push((
                constraint.id,
                constraint.variable.0,
                binding.clone(),
            )),
            UnaryConstraintKind::ExportName { export_name } => ascent.required_export.push((
                constraint.id,
                constraint.variable.0,
                export_name.clone(),
            )),
            UnaryConstraintKind::Kind { statement_kind } => ascent.required_kind.push((
                constraint.id,
                constraint.variable.0,
                statement_kind.clone(),
            )),
            UnaryConstraintKind::ReferencesBinding { binding, edge_kind } => {
                ascent.required_references_binding.push((
                    constraint.id,
                    constraint.variable.0,
                    binding.clone(),
                    edge_kind.clone(),
                ));
            }
            UnaryConstraintKind::ReadsMember { member } => ascent.required_reads_member.push((
                constraint.id,
                constraint.variable.0,
                member.clone(),
            )),
            UnaryConstraintKind::ReadsMemberFromBinding { object, member } => {
                ascent.required_reads_member_from_binding.push((
                    constraint.id,
                    constraint.variable.0,
                    object.clone(),
                    member.clone(),
                ));
            }
            UnaryConstraintKind::ConsumesModuleMember { module, member } => {
                ascent.required_consumes_module_member.push((
                    constraint.id,
                    constraint.variable.0,
                    module.clone(),
                    member.clone(),
                ));
            }
            UnaryConstraintKind::PassedToCall {
                callee_member,
                arg_index,
            } => ascent.required_passed_to_call.push((
                constraint.id,
                constraint.variable.0,
                callee_member.clone(),
                *arg_index,
            )),
            UnaryConstraintKind::PassedToCallFromBinding {
                callee_object,
                callee_member,
                arg_index,
            } => ascent.required_passed_to_call_from_binding.push((
                constraint.id,
                constraint.variable.0,
                callee_object.clone(),
                callee_member.clone(),
                *arg_index,
            )),
            UnaryConstraintKind::MakesDecorateCallForBinding {
                class_anchor,
                member,
            } => ascent.required_makes_decorate_call_for_binding.push((
                constraint.id,
                constraint.variable.0,
                class_anchor.clone(),
                member.clone(),
            )),
            UnaryConstraintKind::SourceMatchCandidate { selector_key } => {
                ascent.required_source_match_candidate.push((
                    constraint.id,
                    constraint.variable.0,
                    selector_key.clone(),
                ));
            }
            UnaryConstraintKind::OwnerStatementOrdinal { ordinal } => {
                ascent.required_owner_statement_ordinal.push((
                    constraint.id,
                    constraint.variable.0,
                    ordinal.0,
                ));
            }
        }
    }
    for constraint in &support.node_unary_constraints {
        match &constraint.kind {
            NodeUnaryConstraintKind::Kind { node_kind } => ascent.required_ast_kind.push((
                constraint.id,
                constraint.variable.0,
                node_kind.clone(),
            )),
            NodeUnaryConstraintKind::ChildCount { count } => ascent
                .required_ast_child_count
                .push((constraint.id, constraint.variable.0, *count)),
            NodeUnaryConstraintKind::ChildParent { index, child } => {
                ascent.required_ast_child_parent.push((
                    constraint.id,
                    constraint.variable.0,
                    *index,
                    *child,
                ));
            }
            NodeUnaryConstraintKind::ChildChild { parent, index } => {
                ascent.required_ast_child_child.push((
                    constraint.id,
                    constraint.variable.0,
                    *parent,
                    *index,
                ));
            }
            NodeUnaryConstraintKind::StringLiteral { value } => {
                ascent.required_ast_string_literal.push((
                    constraint.id,
                    constraint.variable.0,
                    value.clone(),
                ));
            }
            NodeUnaryConstraintKind::NumberLiteral { value } => {
                ascent.required_ast_number_literal.push((
                    constraint.id,
                    constraint.variable.0,
                    value.clone(),
                ));
            }
            NodeUnaryConstraintKind::BoolLiteral { value } => {
                ascent.required_ast_bool_literal.push((
                    constraint.id,
                    constraint.variable.0,
                    *value,
                ));
            }
            NodeUnaryConstraintKind::IdentifierName { value } => {
                ascent.required_ast_identifier_name.push((
                    constraint.id,
                    constraint.variable.0,
                    value.clone(),
                ));
            }
            NodeUnaryConstraintKind::PropertyName { value } => {
                ascent.required_ast_property_name.push((
                    constraint.id,
                    constraint.variable.0,
                    value.clone(),
                ));
            }
            NodeUnaryConstraintKind::Operator { value } => {
                ascent.required_ast_operator.push((
                    constraint.id,
                    constraint.variable.0,
                    value.clone(),
                ));
            }
            NodeUnaryConstraintKind::RegexLiteral { pattern, flags } => {
                ascent.required_ast_regex_literal.push((
                    constraint.id,
                    constraint.variable.0,
                    pattern.clone(),
                    flags.clone(),
                ));
            }
            NodeUnaryConstraintKind::TopLevelOrdinal { ordinal } => {
                ascent.required_ast_top_level_ordinal.push((
                    constraint.id,
                    constraint.variable.0,
                    ordinal.0,
                ));
            }
        }
    }
    for constraint in &support.ordinal_unary_constraints {
        match &constraint.kind {
            OrdinalUnaryConstraintKind::OwnerStatementOrdinal { owner } => {
                ascent.required_statement_ordinal_for_owner.push((
                    constraint.id,
                    constraint.variable.0,
                    owner.0,
                ));
            }
            OrdinalUnaryConstraintKind::AstTopLevelNode { node } => {
                ascent.required_ast_top_level_node.push((
                    constraint.id,
                    constraint.variable.0,
                    *node,
                ));
            }
        }
    }
    for constraint in &support.binary_constraints {
        match &constraint.kind {
            BinaryConstraintKind::ReferencesOwner => ascent.required_references_owner.push((
                constraint.id,
                constraint.left.0,
                constraint.right.0,
            )),
            BinaryConstraintKind::AliasesOwner => ascent.required_aliases_owner.push((
                constraint.id,
                constraint.left.0,
                constraint.right.0,
            )),
            BinaryConstraintKind::ReadsMemberOfOwner { member } => {
                ascent.required_reads_member_of_owner.push((
                    constraint.id,
                    constraint.left.0,
                    constraint.right.0,
                    member.clone(),
                ));
            }
            BinaryConstraintKind::PassedToCallOfOwner {
                callee_member,
                arg_index,
            } => ascent.required_passed_to_call_of_owner.push((
                constraint.id,
                constraint.left.0,
                constraint.right.0,
                callee_member.clone(),
                *arg_index,
            )),
            BinaryConstraintKind::MakesDecorateCallForOwner { member } => {
                ascent.required_makes_decorate_call_for_owner.push((
                    constraint.id,
                    constraint.left.0,
                    constraint.right.0,
                    member.clone(),
                ));
            }
            BinaryConstraintKind::IntrinsicAlias { property } => {
                ascent.required_intrinsic_alias.push((
                    constraint.id,
                    constraint.left.0,
                    constraint.right.0,
                    property.clone(),
                ));
            }
        }
    }
    for constraint in &support.owner_ordinal_constraints {
        match constraint.kind {
            OwnerOrdinalConstraintKind::OwnerStatementOrdinal => {
                ascent.required_owner_statement_ordinal_var.push((
                    constraint.id,
                    constraint.owner.0,
                    constraint.ordinal.0,
                ));
            }
        }
    }
    for constraint in &support.node_node_constraints {
        match constraint.kind {
            NodeNodeConstraintKind::AstChild { index } => ascent.required_ast_child.push((
                constraint.id,
                constraint.left.0,
                constraint.right.0,
                index,
            )),
        }
    }
    for constraint in &support.node_ordinal_constraints {
        match constraint.kind {
            NodeOrdinalConstraintKind::AstTopLevel => ascent.required_ast_top_level.push((
                constraint.id,
                constraint.node.0,
                constraint.ordinal.0,
            )),
        }
    }
    for constraint in &support.node_string_constraints {
        match constraint.kind {
            NodeStringConstraintKind::AstStringLiteral => {
                ascent.required_ast_string_literal_var.push((
                    constraint.id,
                    constraint.node.0,
                    constraint.string.0,
                ));
            }
        }
    }
    ascent.run();

    let unary_supports = group_unary_supports(ascent.constraint_support);
    let node_unary_supports = group_node_unary_supports(ascent.node_constraint_support);
    let ordinal_unary_supports = group_ordinal_unary_supports(ascent.ordinal_constraint_support);
    let binary_edges = group_binary_edges(ascent.binary_constraint_edge);
    let owner_ordinal_edges = group_owner_ordinal_edges(ascent.owner_ordinal_constraint_edge);
    let node_node_edges = group_node_node_edges(ascent.node_node_constraint_edge);
    let node_ordinal_edges = group_node_ordinal_edges(ascent.node_ordinal_constraint_edge);
    let node_string_edges = group_node_string_edges(ascent.node_string_constraint_edge);
    let derived_support = DerivedSupport {
        owner_unary: unary_supports,
        node_unary: node_unary_supports,
        ordinal_unary: ordinal_unary_supports,
        owner_owner: binary_edges,
        owner_ordinal: owner_ordinal_edges,
        node_node: node_node_edges,
        node_ordinal: node_ordinal_edges,
        node_string: node_string_edges,
    };
    let candidates = solve_candidate_sets(program, &support, &fact_index, derived_support);

    let mut claims = Vec::new();
    for target in &program.targets {
        if let Some(reason) = support.unsupported_reason_for_target(target) {
            claims.push(SolverClaim {
                target: target.id,
                outcome: ClaimOutcome::Unsupported { message: reason },
            });
            continue;
        }

        let candidates = candidates
            .owners
            .get(&target.owner)
            .cloned()
            .unwrap_or_default();
        let outcome = classify_candidates(target, candidates, &fact_index, &support);
        claims.push(SolverClaim {
            target: target.id,
            outcome,
        });
    }

    let mut result = SolverResult { claims };
    apply_all_different(program, &mut result);
    Ok(result)
}

fn unsupported_result(program: &SelectorProgram, message: String) -> SolverResult {
    SolverResult {
        claims: program
            .targets
            .iter()
            .map(|target| SolverClaim {
                target: target.id,
                outcome: ClaimOutcome::Unsupported {
                    message: message.clone(),
                },
            })
            .collect(),
    }
}

fn group_unary_supports(rows: Vec<(usize, usize, usize)>) -> BTreeMap<usize, BTreeSet<OwnerId>> {
    let mut grouped = BTreeMap::new();
    for (constraint, _var, owner) in rows {
        grouped
            .entry(constraint)
            .or_insert_with(BTreeSet::new)
            .insert(OwnerId(owner));
    }
    grouped
}

fn group_binary_edges(
    rows: Vec<(usize, usize, usize, usize, usize)>,
) -> BTreeMap<usize, BTreeSet<(OwnerId, OwnerId)>> {
    let mut grouped = BTreeMap::new();
    for (constraint, _left_var, left_owner, _right_var, right_owner) in rows {
        grouped
            .entry(constraint)
            .or_insert_with(BTreeSet::new)
            .insert((OwnerId(left_owner), OwnerId(right_owner)));
    }
    grouped
}

fn group_node_unary_supports(rows: Vec<(usize, usize, u32)>) -> BTreeMap<usize, BTreeSet<u32>> {
    let mut grouped = BTreeMap::new();
    for (constraint, _var, node) in rows {
        grouped
            .entry(constraint)
            .or_insert_with(BTreeSet::new)
            .insert(node);
    }
    grouped
}

fn group_ordinal_unary_supports(
    rows: Vec<(usize, usize, usize)>,
) -> BTreeMap<usize, BTreeSet<StatementOrdinal>> {
    let mut grouped = BTreeMap::new();
    for (constraint, _var, ordinal) in rows {
        grouped
            .entry(constraint)
            .or_insert_with(BTreeSet::new)
            .insert(StatementOrdinal(ordinal));
    }
    grouped
}

fn group_owner_ordinal_edges(
    rows: Vec<(usize, usize, usize, usize, usize)>,
) -> BTreeMap<usize, BTreeSet<(OwnerId, StatementOrdinal)>> {
    let mut grouped = BTreeMap::new();
    for (constraint, _owner_var, owner, _ordinal_var, ordinal) in rows {
        grouped
            .entry(constraint)
            .or_insert_with(BTreeSet::new)
            .insert((OwnerId(owner), StatementOrdinal(ordinal)));
    }
    grouped
}

fn group_node_node_edges(
    rows: Vec<(usize, usize, u32, usize, u32)>,
) -> BTreeMap<usize, BTreeSet<(u32, u32)>> {
    let mut grouped = BTreeMap::new();
    for (constraint, _left_var, left, _right_var, right) in rows {
        grouped
            .entry(constraint)
            .or_insert_with(BTreeSet::new)
            .insert((left, right));
    }
    grouped
}

fn group_node_ordinal_edges(
    rows: Vec<(usize, usize, u32, usize, usize)>,
) -> BTreeMap<usize, BTreeSet<(u32, StatementOrdinal)>> {
    let mut grouped = BTreeMap::new();
    for (constraint, _node_var, node, _ordinal_var, ordinal) in rows {
        grouped
            .entry(constraint)
            .or_insert_with(BTreeSet::new)
            .insert((node, StatementOrdinal(ordinal)));
    }
    grouped
}

fn group_node_string_edges(
    rows: Vec<(usize, usize, u32, usize, String)>,
) -> BTreeMap<usize, BTreeSet<(u32, String)>> {
    let mut grouped = BTreeMap::new();
    for (constraint, _node_var, node, _string_var, value) in rows {
        grouped
            .entry(constraint)
            .or_insert_with(BTreeSet::new)
            .insert((node, value));
    }
    grouped
}

#[derive(Debug, Default)]
struct CandidateSets {
    owners: BTreeMap<SelectorVariableId, BTreeSet<OwnerId>>,
    nodes: BTreeMap<SelectorVariableId, BTreeSet<u32>>,
    ordinals: BTreeMap<SelectorVariableId, BTreeSet<StatementOrdinal>>,
    strings: BTreeMap<SelectorVariableId, BTreeSet<String>>,
}

struct DerivedSupport {
    owner_unary: BTreeMap<usize, BTreeSet<OwnerId>>,
    node_unary: BTreeMap<usize, BTreeSet<u32>>,
    ordinal_unary: BTreeMap<usize, BTreeSet<StatementOrdinal>>,
    owner_owner: BTreeMap<usize, BTreeSet<(OwnerId, OwnerId)>>,
    owner_ordinal: BTreeMap<usize, BTreeSet<(OwnerId, StatementOrdinal)>>,
    node_node: BTreeMap<usize, BTreeSet<(u32, u32)>>,
    node_ordinal: BTreeMap<usize, BTreeSet<(u32, StatementOrdinal)>>,
    node_string: BTreeMap<usize, BTreeSet<(u32, String)>>,
}

fn solve_candidate_sets(
    program: &SelectorProgram,
    support: &ProgramSupport,
    facts: &FactIndex,
    derived: DerivedSupport,
) -> CandidateSets {
    let mut candidates = CandidateSets::default();
    for variable in &program.variables {
        match variable.domain {
            VariableDomain::Owner => {
                candidates
                    .owners
                    .insert(variable.id, facts.all_owners.clone());
            }
            VariableDomain::AstNode => {
                candidates
                    .nodes
                    .insert(variable.id, facts.all_nodes.clone());
            }
            VariableDomain::StatementOrdinal => {
                candidates
                    .ordinals
                    .insert(variable.id, facts.all_statement_ordinals.clone());
            }
            VariableDomain::String => {
                candidates
                    .strings
                    .insert(variable.id, facts.all_strings.clone());
            }
        }
    }

    for (variable, constraints) in &support.unary_constraints_by_var {
        intersect_constraints(
            &mut candidates.owners,
            *variable,
            constraints,
            &derived.owner_unary,
        );
    }
    for (variable, constraints) in &support.node_unary_constraints_by_var {
        intersect_constraints(
            &mut candidates.nodes,
            *variable,
            constraints,
            &derived.node_unary,
        );
    }
    for (variable, constraints) in &support.ordinal_unary_constraints_by_var {
        intersect_constraints(
            &mut candidates.ordinals,
            *variable,
            constraints,
            &derived.ordinal_unary,
        );
    }

    let mut changed = true;
    while changed {
        changed = false;
        for constraint in &support.binary_constraints {
            let edges = derived
                .owner_owner
                .get(&constraint.id)
                .cloned()
                .unwrap_or_default();
            let left_current = candidates
                .owners
                .get(&constraint.left)
                .cloned()
                .unwrap_or_default();
            let right_current = candidates
                .owners
                .get(&constraint.right)
                .cloned()
                .unwrap_or_default();

            // Owner-owner selector edges are directional: the left side is the
            // selector target, while the right side is an anchor target. A failed
            // dependent selector must not erase the anchor's own independent
            // claim; AST/internal variables below remain bidirectional.
            let left_allowed: BTreeSet<_> = edges
                .iter()
                .filter(|(left, right)| {
                    left_current.contains(left) && right_current.contains(right)
                })
                .map(|(left, _right)| *left)
                .collect();

            if intersect_candidate_set(&mut candidates.owners, constraint.left, left_allowed) {
                changed = true;
            }
        }
        for constraint in &support.owner_ordinal_constraints {
            let edges = derived
                .owner_ordinal
                .get(&constraint.id)
                .cloned()
                .unwrap_or_default();
            let owner_current = candidates
                .owners
                .get(&constraint.owner)
                .cloned()
                .unwrap_or_default();
            let ordinal_current = candidates
                .ordinals
                .get(&constraint.ordinal)
                .cloned()
                .unwrap_or_default();
            let (owner_allowed, ordinal_allowed): (BTreeSet<_>, BTreeSet<_>) = edges
                .iter()
                .filter(|(owner, ordinal)| {
                    owner_current.contains(owner) && ordinal_current.contains(ordinal)
                })
                .map(|(owner, ordinal)| (*owner, *ordinal))
                .unzip();
            if intersect_candidate_set(&mut candidates.owners, constraint.owner, owner_allowed) {
                changed = true;
            }
            if intersect_candidate_set(
                &mut candidates.ordinals,
                constraint.ordinal,
                ordinal_allowed,
            ) {
                changed = true;
            }
        }
        for constraint in &support.node_node_constraints {
            let edges = derived
                .node_node
                .get(&constraint.id)
                .cloned()
                .unwrap_or_default();
            let left_current = candidates
                .nodes
                .get(&constraint.left)
                .cloned()
                .unwrap_or_default();
            let right_current = candidates
                .nodes
                .get(&constraint.right)
                .cloned()
                .unwrap_or_default();
            let (left_allowed, right_allowed): (BTreeSet<_>, BTreeSet<_>) = edges
                .iter()
                .filter(|(left, right)| {
                    left_current.contains(left) && right_current.contains(right)
                })
                .map(|(left, right)| (*left, *right))
                .unzip();
            if intersect_candidate_set(&mut candidates.nodes, constraint.left, left_allowed) {
                changed = true;
            }
            if intersect_candidate_set(&mut candidates.nodes, constraint.right, right_allowed) {
                changed = true;
            }
        }
        for constraint in &support.node_ordinal_constraints {
            let edges = derived
                .node_ordinal
                .get(&constraint.id)
                .cloned()
                .unwrap_or_default();
            let node_current = candidates
                .nodes
                .get(&constraint.node)
                .cloned()
                .unwrap_or_default();
            let ordinal_current = candidates
                .ordinals
                .get(&constraint.ordinal)
                .cloned()
                .unwrap_or_default();
            let (node_allowed, ordinal_allowed): (BTreeSet<_>, BTreeSet<_>) = edges
                .iter()
                .filter(|(node, ordinal)| {
                    node_current.contains(node) && ordinal_current.contains(ordinal)
                })
                .map(|(node, ordinal)| (*node, *ordinal))
                .unzip();
            if intersect_candidate_set(&mut candidates.nodes, constraint.node, node_allowed) {
                changed = true;
            }
            if intersect_candidate_set(
                &mut candidates.ordinals,
                constraint.ordinal,
                ordinal_allowed,
            ) {
                changed = true;
            }
        }
        for constraint in &support.node_string_constraints {
            let edges = derived
                .node_string
                .get(&constraint.id)
                .cloned()
                .unwrap_or_default();
            let node_current = candidates
                .nodes
                .get(&constraint.node)
                .cloned()
                .unwrap_or_default();
            let string_current = candidates
                .strings
                .get(&constraint.string)
                .cloned()
                .unwrap_or_default();
            let (node_allowed, string_allowed): (BTreeSet<_>, BTreeSet<_>) = edges
                .iter()
                .filter(|(node, value)| {
                    node_current.contains(node) && string_current.contains(value)
                })
                .map(|(node, value)| (*node, value.clone()))
                .unzip();
            if intersect_candidate_set(&mut candidates.nodes, constraint.node, node_allowed) {
                changed = true;
            }
            if intersect_candidate_set(&mut candidates.strings, constraint.string, string_allowed) {
                changed = true;
            }
        }
    }

    candidates
}

fn intersect_constraints<T: Clone + Ord>(
    candidates: &mut BTreeMap<SelectorVariableId, BTreeSet<T>>,
    variable: SelectorVariableId,
    constraints: &[usize],
    supports: &BTreeMap<usize, BTreeSet<T>>,
) {
    let mut intersection: Option<BTreeSet<T>> = None;
    for constraint in constraints {
        let supported = supports.get(constraint).cloned().unwrap_or_default();
        intersection = Some(match intersection {
            Some(existing) => existing.intersection(&supported).cloned().collect(),
            None => supported,
        });
    }
    if let Some(intersection) = intersection {
        candidates.insert(variable, intersection);
    }
}

fn intersect_candidate_set<T: Clone + Ord>(
    candidates: &mut BTreeMap<SelectorVariableId, BTreeSet<T>>,
    variable: SelectorVariableId,
    allowed: BTreeSet<T>,
) -> bool {
    let current = candidates.entry(variable).or_default();
    let next = current.intersection(&allowed).cloned().collect();
    if *current == next {
        return false;
    }
    *current = next;
    true
}

fn classify_candidates(
    target: &SelectorTarget,
    candidates: BTreeSet<OwnerId>,
    facts: &FactIndex,
    support: &ProgramSupport,
) -> ClaimOutcome {
    let claims: Vec<ResolvedClaim> =
        if let Some(selector_key) = support.source_match_selector_for(target.owner) {
            candidates
                .into_iter()
                .flat_map(|owner| resolved_source_match_claims(target, owner, facts, &selector_key))
                .collect()
        } else {
            candidates
                .into_iter()
                .filter_map(|owner| resolved_claim(target, owner, facts, support))
                .collect()
        };
    match claims.as_slice() {
        [] => ClaimOutcome::NoMatch,
        [claim] => ClaimOutcome::Unique {
            claim: claim.clone(),
        },
        _ => ClaimOutcome::Ambiguous { candidates: claims },
    }
}

fn resolved_source_match_claims(
    target: &SelectorTarget,
    owner: OwnerId,
    facts: &FactIndex,
    selector_key: &str,
) -> Vec<ResolvedClaim> {
    let Some(statement_ordinal) = facts.statement_ordinal_by_owner.get(&owner).copied() else {
        return Vec::new();
    };
    facts
        .source_match_bindings_for_owner(selector_key, owner)
        .into_iter()
        .map(|binding| ResolvedClaim {
            chunk_id: target.chunk_id,
            owner,
            statement_ordinal,
            binding: Some(binding),
            provenance: Vec::new(),
        })
        .collect()
}

fn resolved_claim(
    target: &SelectorTarget,
    owner: OwnerId,
    facts: &FactIndex,
    support: &ProgramSupport,
) -> Option<ResolvedClaim> {
    let statement_ordinal = facts.statement_ordinal_by_owner.get(&owner).copied()?;
    let binding = support.binding_constraint_for(target.owner).or_else(|| {
        support
            .source_match_selector_for(target.owner)
            .and_then(|selector_key| facts.source_match_binding_for_owner(&selector_key, owner))
            .or_else(|| {
                facts
                    .single_binding_for_owner(owner)
                    .map(ToString::to_string)
            })
    });
    if matches!(
        target.claim,
        ClaimKind::Binding { .. } | ClaimKind::BindingGroupMember { .. }
    ) && binding.is_none()
    {
        return None;
    }
    Some(ResolvedClaim {
        chunk_id: target.chunk_id,
        owner,
        statement_ordinal,
        binding,
        provenance: Vec::new(),
    })
}

fn apply_all_different(program: &SelectorProgram, result: &mut SolverResult) {
    for target_set in &program.all_different {
        let mut owners: BTreeMap<OwnerId, Vec<SelectorTargetId>> = BTreeMap::new();
        for target_id in target_set {
            let Some(ClaimOutcome::Unique { claim }) = result.outcome_for(*target_id) else {
                continue;
            };
            owners.entry(claim.owner).or_default().push(*target_id);
        }

        for (owner, conflicting_targets) in owners {
            if conflicting_targets.len() < 2 {
                continue;
            }
            for target_id in &conflicting_targets {
                if let Some(claim) = result
                    .claims
                    .iter_mut()
                    .find(|claim| claim.target == *target_id)
                {
                    claim.outcome = ClaimOutcome::Duplicate {
                        owner,
                        conflicting_targets: conflicting_targets.clone(),
                    };
                }
            }
        }
    }
}

#[derive(Debug, Default)]
struct ProgramSupport {
    next_constraint_id: usize,
    unary_constraints: Vec<UnaryConstraint>,
    node_unary_constraints: Vec<NodeUnaryConstraint>,
    ordinal_unary_constraints: Vec<OrdinalUnaryConstraint>,
    binary_constraints: Vec<BinaryConstraint>,
    owner_ordinal_constraints: Vec<OwnerOrdinalConstraint>,
    node_node_constraints: Vec<NodeNodeConstraint>,
    node_ordinal_constraints: Vec<NodeOrdinalConstraint>,
    node_string_constraints: Vec<NodeStringConstraint>,
    unary_constraints_by_var: BTreeMap<SelectorVariableId, Vec<usize>>,
    node_unary_constraints_by_var: BTreeMap<SelectorVariableId, Vec<usize>>,
    ordinal_unary_constraints_by_var: BTreeMap<SelectorVariableId, Vec<usize>>,
    constraints_by_var: BTreeMap<SelectorVariableId, Vec<usize>>,
    unsupported_atoms: Vec<String>,
}

impl ProgramSupport {
    fn from_program(program: &SelectorProgram) -> Self {
        let mut support = Self::default();

        for atom in &program.atoms {
            match atom {
                SelectorAtom::OwnerDeclaresBinding {
                    owner: OwnerTerm::Var { id },
                    binding: StringTerm::Const { value },
                } => support.add_unary(
                    *id,
                    UnaryConstraintKind::Binding {
                        binding: value.clone(),
                    },
                ),
                SelectorAtom::OwnerExportName {
                    owner: OwnerTerm::Var { id },
                    export_name: StringTerm::Const { value },
                } => support.add_unary(
                    *id,
                    UnaryConstraintKind::ExportName {
                        export_name: value.clone(),
                    },
                ),
                SelectorAtom::OwnerKind {
                    owner: OwnerTerm::Var { id },
                    statement_kind: StringTerm::Const { value },
                } => support.add_unary(
                    *id,
                    UnaryConstraintKind::Kind {
                        statement_kind: value.clone(),
                    },
                ),
                SelectorAtom::OwnerStatementOrdinal { owner, ordinal } => {
                    support.add_owner_statement_ordinal(owner, ordinal)
                }
                SelectorAtom::OwnerReferencesBinding {
                    owner: OwnerTerm::Var { id },
                    binding: StringTerm::Const { value },
                    edge_kind,
                } => match optional_const_string(edge_kind) {
                    Some(edge_kind) => support.add_unary(
                        *id,
                        UnaryConstraintKind::ReferencesBinding {
                            binding: value.clone(),
                            edge_kind,
                        },
                    ),
                    None => support.unsupported_atoms.push(
                        "unsupported non-constant owner_references_binding.edge_kind".to_string(),
                    ),
                },
                SelectorAtom::OwnerReferencesOwner {
                    owner: OwnerTerm::Var { id: owner },
                    referenced: OwnerTerm::Var { id: referenced },
                } => support.add_binary(*owner, *referenced, BinaryConstraintKind::ReferencesOwner),
                SelectorAtom::OwnerAliasesOwner {
                    owner: OwnerTerm::Var { id: owner },
                    aliased: OwnerTerm::Var { id: aliased },
                } => support.add_binary(*owner, *aliased, BinaryConstraintKind::AliasesOwner),
                SelectorAtom::AstKind { node, node_kind } => match node {
                    NodeTerm::Var { id } => support.add_node_unary(
                        *id,
                        NodeUnaryConstraintKind::Kind {
                            node_kind: node_kind.as_tag().to_string(),
                        },
                    ),
                    NodeTerm::Const { .. } => support
                        .unsupported_atoms
                        .push("unsupported constant-only ast_kind assertion".to_string()),
                },
                SelectorAtom::AstChildCount { node, count } => match node {
                    NodeTerm::Var { id } => support
                        .add_node_unary(*id, NodeUnaryConstraintKind::ChildCount { count: *count }),
                    NodeTerm::Const { .. } => support
                        .unsupported_atoms
                        .push("unsupported constant-only ast_child_count assertion".to_string()),
                },
                SelectorAtom::AstChild {
                    parent,
                    index,
                    child,
                } => support.add_ast_child(parent, *index, child),
                SelectorAtom::AstStringLiteral {
                    node,
                    value: StringTerm::Const { value },
                } => support.add_node_label(
                    node,
                    NodeUnaryConstraintKind::StringLiteral {
                        value: value.clone(),
                    },
                    "ast_string_literal",
                ),
                SelectorAtom::AstStringLiteral {
                    node: NodeTerm::Var { id: node },
                    value: StringTerm::Var { id: string },
                } => support.add_node_string(
                    *node,
                    *string,
                    NodeStringConstraintKind::AstStringLiteral,
                ),
                SelectorAtom::AstNumberLiteral {
                    node,
                    value: StringTerm::Const { value },
                } => support.add_node_label(
                    node,
                    NodeUnaryConstraintKind::NumberLiteral {
                        value: value.clone(),
                    },
                    "ast_number_literal",
                ),
                SelectorAtom::AstBoolLiteral { node, value } => support.add_node_label(
                    node,
                    NodeUnaryConstraintKind::BoolLiteral { value: *value },
                    "ast_bool_literal",
                ),
                SelectorAtom::AstIdentifierName {
                    node,
                    value: StringTerm::Const { value },
                } => support.add_node_label(
                    node,
                    NodeUnaryConstraintKind::IdentifierName {
                        value: value.clone(),
                    },
                    "ast_identifier_name",
                ),
                SelectorAtom::AstPropertyName {
                    node,
                    value: StringTerm::Const { value },
                } => support.add_node_label(
                    node,
                    NodeUnaryConstraintKind::PropertyName {
                        value: value.clone(),
                    },
                    "ast_property_name",
                ),
                SelectorAtom::AstOperator {
                    node,
                    value: StringTerm::Const { value },
                } => support.add_node_label(
                    node,
                    NodeUnaryConstraintKind::Operator {
                        value: value.clone(),
                    },
                    "ast_operator",
                ),
                SelectorAtom::AstRegexLiteral {
                    node,
                    pattern: StringTerm::Const { value: pattern },
                    flags: StringTerm::Const { value: flags },
                } => support.add_node_label(
                    node,
                    NodeUnaryConstraintKind::RegexLiteral {
                        pattern: pattern.clone(),
                        flags: flags.clone(),
                    },
                    "ast_regex_literal",
                ),
                SelectorAtom::AstTopLevel { node, ordinal } => {
                    support.add_ast_top_level(node, ordinal)
                }
                SelectorAtom::ReadsMember {
                    owner: OwnerTerm::Var { id },
                    object: None,
                    member: StringTerm::Const { value },
                } => support.add_unary(
                    *id,
                    UnaryConstraintKind::ReadsMember {
                        member: value.clone(),
                    },
                ),
                SelectorAtom::ReadsMember {
                    owner: OwnerTerm::Var { id },
                    object:
                        Some(StringTerm::Const {
                            value: object_value,
                        }),
                    member: StringTerm::Const { value },
                } => support.add_unary(
                    *id,
                    UnaryConstraintKind::ReadsMemberFromBinding {
                        object: object_value.clone(),
                        member: value.clone(),
                    },
                ),
                SelectorAtom::ReadsMemberOfOwner {
                    owner: OwnerTerm::Var { id: owner },
                    object: OwnerTerm::Var { id: object },
                    member: StringTerm::Const { value },
                } => support.add_binary(
                    *owner,
                    *object,
                    BinaryConstraintKind::ReadsMemberOfOwner {
                        member: value.clone(),
                    },
                ),
                SelectorAtom::ConsumesModuleMember {
                    owner: OwnerTerm::Var { id },
                    module: StringTerm::Const { value: module },
                    member: StringTerm::Const { value: member },
                } => support.add_unary(
                    *id,
                    UnaryConstraintKind::ConsumesModuleMember {
                        module: module.clone(),
                        member: member.clone(),
                    },
                ),
                SelectorAtom::PassedToCall {
                    owner: OwnerTerm::Var { id },
                    callee_object: None,
                    callee_member: StringTerm::Const { value },
                    arg_index,
                } => support.add_unary(
                    *id,
                    UnaryConstraintKind::PassedToCall {
                        callee_member: value.clone(),
                        arg_index: *arg_index,
                    },
                ),
                SelectorAtom::PassedToCall {
                    owner: OwnerTerm::Var { id },
                    callee_object:
                        Some(StringTerm::Const {
                            value: object_value,
                        }),
                    callee_member: StringTerm::Const { value },
                    arg_index,
                } => support.add_unary(
                    *id,
                    UnaryConstraintKind::PassedToCallFromBinding {
                        callee_object: object_value.clone(),
                        callee_member: value.clone(),
                        arg_index: *arg_index,
                    },
                ),
                SelectorAtom::PassedToCallOfOwner {
                    owner: OwnerTerm::Var { id: owner },
                    callee_object: OwnerTerm::Var { id: object },
                    callee_member: StringTerm::Const { value },
                    arg_index,
                } => support.add_binary(
                    *owner,
                    *object,
                    BinaryConstraintKind::PassedToCallOfOwner {
                        callee_member: value.clone(),
                        arg_index: *arg_index,
                    },
                ),
                SelectorAtom::MakesDecorateCall {
                    owner: OwnerTerm::Var { id },
                    class_anchor: StringTerm::Const { value },
                    member,
                } => match optional_const_string(member) {
                    Some(member) => support.add_unary(
                        *id,
                        UnaryConstraintKind::MakesDecorateCallForBinding {
                            class_anchor: value.clone(),
                            member,
                        },
                    ),
                    None => support
                        .unsupported_atoms
                        .push("unsupported non-constant makes_decorate_call.member".to_string()),
                },
                SelectorAtom::MakesDecorateCallForOwner {
                    owner: OwnerTerm::Var { id: owner },
                    class_anchor: OwnerTerm::Var { id: class_anchor },
                    member,
                } => match optional_const_string(member) {
                    Some(member) => support.add_binary(
                        *owner,
                        *class_anchor,
                        BinaryConstraintKind::MakesDecorateCallForOwner { member },
                    ),
                    None => support.unsupported_atoms.push(
                        "unsupported non-constant makes_decorate_call_for_owner.member".to_string(),
                    ),
                },
                SelectorAtom::IntrinsicAlias {
                    owner: OwnerTerm::Var { id: owner },
                    property: StringTerm::Const { value },
                    referenced_by: OwnerTerm::Var { id: referenced_by },
                } => support.add_binary(
                    *owner,
                    *referenced_by,
                    BinaryConstraintKind::IntrinsicAlias {
                        property: value.clone(),
                    },
                ),
                SelectorAtom::SourceMatchCandidate {
                    owner: OwnerTerm::Var { id },
                    selector_key: StringTerm::Const { value },
                } => support.add_unary(
                    *id,
                    UnaryConstraintKind::SourceMatchCandidate {
                        selector_key: value.clone(),
                    },
                ),
                unsupported => support.unsupported_atoms.push(format!(
                    "unsupported selector atom `{}`",
                    atom_kind(unsupported)
                )),
            }
        }
        support
    }

    fn add_unary(&mut self, variable: SelectorVariableId, kind: UnaryConstraintKind) {
        let id = self.next_constraint_id;
        self.next_constraint_id += 1;
        self.unary_constraints
            .push(UnaryConstraint { id, variable, kind });
        self.unary_constraints_by_var
            .entry(variable)
            .or_default()
            .push(id);
        self.constraints_by_var
            .entry(variable)
            .or_default()
            .push(id);
    }

    fn add_binary(
        &mut self,
        left: SelectorVariableId,
        right: SelectorVariableId,
        kind: BinaryConstraintKind,
    ) {
        let id = self.next_constraint_id;
        self.next_constraint_id += 1;
        self.binary_constraints.push(BinaryConstraint {
            id,
            left,
            right,
            kind,
        });
        self.constraints_by_var.entry(left).or_default().push(id);
        self.constraints_by_var.entry(right).or_default().push(id);
    }

    fn add_node_unary(&mut self, variable: SelectorVariableId, kind: NodeUnaryConstraintKind) {
        let id = self.next_constraint_id;
        self.next_constraint_id += 1;
        self.node_unary_constraints
            .push(NodeUnaryConstraint { id, variable, kind });
        self.node_unary_constraints_by_var
            .entry(variable)
            .or_default()
            .push(id);
        self.constraints_by_var
            .entry(variable)
            .or_default()
            .push(id);
    }

    fn add_ordinal_unary(
        &mut self,
        variable: SelectorVariableId,
        kind: OrdinalUnaryConstraintKind,
    ) {
        let id = self.next_constraint_id;
        self.next_constraint_id += 1;
        self.ordinal_unary_constraints
            .push(OrdinalUnaryConstraint { id, variable, kind });
        self.ordinal_unary_constraints_by_var
            .entry(variable)
            .or_default()
            .push(id);
        self.constraints_by_var
            .entry(variable)
            .or_default()
            .push(id);
    }

    fn add_owner_ordinal(
        &mut self,
        owner: SelectorVariableId,
        ordinal: SelectorVariableId,
        kind: OwnerOrdinalConstraintKind,
    ) {
        let id = self.next_constraint_id;
        self.next_constraint_id += 1;
        self.owner_ordinal_constraints.push(OwnerOrdinalConstraint {
            id,
            owner,
            ordinal,
            kind,
        });
        self.constraints_by_var.entry(owner).or_default().push(id);
        self.constraints_by_var.entry(ordinal).or_default().push(id);
    }

    fn add_node_node(
        &mut self,
        left: SelectorVariableId,
        right: SelectorVariableId,
        kind: NodeNodeConstraintKind,
    ) {
        let id = self.next_constraint_id;
        self.next_constraint_id += 1;
        self.node_node_constraints.push(NodeNodeConstraint {
            id,
            left,
            right,
            kind,
        });
        self.constraints_by_var.entry(left).or_default().push(id);
        self.constraints_by_var.entry(right).or_default().push(id);
    }

    fn add_node_ordinal(
        &mut self,
        node: SelectorVariableId,
        ordinal: SelectorVariableId,
        kind: NodeOrdinalConstraintKind,
    ) {
        let id = self.next_constraint_id;
        self.next_constraint_id += 1;
        self.node_ordinal_constraints.push(NodeOrdinalConstraint {
            id,
            node,
            ordinal,
            kind,
        });
        self.constraints_by_var.entry(node).or_default().push(id);
        self.constraints_by_var.entry(ordinal).or_default().push(id);
    }

    fn add_node_string(
        &mut self,
        node: SelectorVariableId,
        string: SelectorVariableId,
        kind: NodeStringConstraintKind,
    ) {
        let id = self.next_constraint_id;
        self.next_constraint_id += 1;
        self.node_string_constraints.push(NodeStringConstraint {
            id,
            node,
            string,
            kind,
        });
        self.constraints_by_var.entry(node).or_default().push(id);
        self.constraints_by_var.entry(string).or_default().push(id);
    }

    fn add_owner_statement_ordinal(&mut self, owner: &OwnerTerm, ordinal: &OrdinalTerm) {
        match (owner, ordinal) {
            (OwnerTerm::Var { id: owner }, OrdinalTerm::Const { ordinal }) => self.add_unary(
                *owner,
                UnaryConstraintKind::OwnerStatementOrdinal { ordinal: *ordinal },
            ),
            (OwnerTerm::Var { id: owner }, OrdinalTerm::Var { id: ordinal }) => self
                .add_owner_ordinal(
                    *owner,
                    *ordinal,
                    OwnerOrdinalConstraintKind::OwnerStatementOrdinal,
                ),
            (OwnerTerm::Const { owner }, OrdinalTerm::Var { id: ordinal }) => self
                .add_ordinal_unary(
                    *ordinal,
                    OrdinalUnaryConstraintKind::OwnerStatementOrdinal { owner: *owner },
                ),
            _ => self
                .unsupported_atoms
                .push("unsupported constant-only owner_statement_ordinal assertion".to_string()),
        }
    }

    fn add_ast_child(&mut self, parent: &NodeTerm, index: u32, child: &NodeTerm) {
        match (parent, child) {
            (NodeTerm::Var { id: parent }, NodeTerm::Var { id: child }) => {
                self.add_node_node(*parent, *child, NodeNodeConstraintKind::AstChild { index });
            }
            (NodeTerm::Var { id: parent }, NodeTerm::Const { node: child }) => self.add_node_unary(
                *parent,
                NodeUnaryConstraintKind::ChildParent {
                    index,
                    child: *child,
                },
            ),
            (NodeTerm::Const { node: parent }, NodeTerm::Var { id: child }) => self.add_node_unary(
                *child,
                NodeUnaryConstraintKind::ChildChild {
                    parent: *parent,
                    index,
                },
            ),
            _ => self
                .unsupported_atoms
                .push("unsupported constant-only ast_child assertion".to_string()),
        }
    }

    fn add_node_label(
        &mut self,
        node: &NodeTerm,
        kind: NodeUnaryConstraintKind,
        relation: &'static str,
    ) {
        match node {
            NodeTerm::Var { id } => self.add_node_unary(*id, kind),
            NodeTerm::Const { .. } => self
                .unsupported_atoms
                .push(format!("unsupported constant-only {relation} assertion")),
        }
    }

    fn add_ast_top_level(&mut self, node: &NodeTerm, ordinal: &OrdinalTerm) {
        match (node, ordinal) {
            (NodeTerm::Var { id: node }, OrdinalTerm::Const { ordinal }) => self.add_node_unary(
                *node,
                NodeUnaryConstraintKind::TopLevelOrdinal { ordinal: *ordinal },
            ),
            (NodeTerm::Var { id: node }, OrdinalTerm::Var { id: ordinal }) => {
                self.add_node_ordinal(*node, *ordinal, NodeOrdinalConstraintKind::AstTopLevel)
            }
            (NodeTerm::Const { node }, OrdinalTerm::Var { id: ordinal }) => self.add_ordinal_unary(
                *ordinal,
                OrdinalUnaryConstraintKind::AstTopLevelNode { node: *node },
            ),
            _ => self
                .unsupported_atoms
                .push("unsupported constant-only ast_top_level assertion".to_string()),
        }
    }

    fn unsupported_reason_for_target(&self, target: &SelectorTarget) -> Option<String> {
        if !self.constraints_by_var.contains_key(&target.owner) {
            return Some("target owner variable has no selector constraints".to_string());
        }
        None
    }

    fn binding_constraint_for(&self, variable: SelectorVariableId) -> Option<String> {
        self.unary_constraints_by_var
            .get(&variable)?
            .iter()
            .filter_map(|constraint_id| {
                let constraint = self
                    .unary_constraints
                    .iter()
                    .find(|constraint| constraint.id == *constraint_id)?;
                match &constraint.kind {
                    UnaryConstraintKind::Binding { binding } => Some(binding.clone()),
                    _ => None,
                }
            })
            .next()
    }

    fn source_match_selector_for(&self, variable: SelectorVariableId) -> Option<String> {
        self.unary_constraints_by_var
            .get(&variable)?
            .iter()
            .filter_map(|constraint_id| {
                let constraint = self
                    .unary_constraints
                    .iter()
                    .find(|constraint| constraint.id == *constraint_id)?;
                match &constraint.kind {
                    UnaryConstraintKind::SourceMatchCandidate { selector_key } => {
                        Some(selector_key.clone())
                    }
                    _ => None,
                }
            })
            .next()
    }
}

#[derive(Debug, Clone)]
struct UnaryConstraint {
    id: usize,
    variable: SelectorVariableId,
    kind: UnaryConstraintKind,
}

#[derive(Debug, Clone)]
enum UnaryConstraintKind {
    Binding {
        binding: String,
    },
    ExportName {
        export_name: String,
    },
    Kind {
        statement_kind: String,
    },
    ReferencesBinding {
        binding: String,
        edge_kind: Option<String>,
    },
    ReadsMember {
        member: String,
    },
    ReadsMemberFromBinding {
        object: String,
        member: String,
    },
    ConsumesModuleMember {
        module: String,
        member: String,
    },
    PassedToCall {
        callee_member: String,
        arg_index: Option<u32>,
    },
    PassedToCallFromBinding {
        callee_object: String,
        callee_member: String,
        arg_index: Option<u32>,
    },
    MakesDecorateCallForBinding {
        class_anchor: String,
        member: Option<String>,
    },
    SourceMatchCandidate {
        selector_key: String,
    },
    OwnerStatementOrdinal {
        ordinal: StatementOrdinal,
    },
}

#[derive(Debug, Clone)]
struct NodeUnaryConstraint {
    id: usize,
    variable: SelectorVariableId,
    kind: NodeUnaryConstraintKind,
}

#[derive(Debug, Clone)]
enum NodeUnaryConstraintKind {
    Kind { node_kind: String },
    ChildCount { count: u32 },
    ChildParent { index: u32, child: u32 },
    ChildChild { parent: u32, index: u32 },
    StringLiteral { value: String },
    NumberLiteral { value: String },
    BoolLiteral { value: bool },
    IdentifierName { value: String },
    PropertyName { value: String },
    Operator { value: String },
    RegexLiteral { pattern: String, flags: String },
    TopLevelOrdinal { ordinal: StatementOrdinal },
}

#[derive(Debug, Clone)]
struct OrdinalUnaryConstraint {
    id: usize,
    variable: SelectorVariableId,
    kind: OrdinalUnaryConstraintKind,
}

#[derive(Debug, Clone)]
enum OrdinalUnaryConstraintKind {
    OwnerStatementOrdinal { owner: OwnerId },
    AstTopLevelNode { node: u32 },
}

#[derive(Debug, Clone)]
struct BinaryConstraint {
    id: usize,
    left: SelectorVariableId,
    right: SelectorVariableId,
    kind: BinaryConstraintKind,
}

#[derive(Debug, Clone)]
enum BinaryConstraintKind {
    ReferencesOwner,
    AliasesOwner,
    ReadsMemberOfOwner {
        member: String,
    },
    PassedToCallOfOwner {
        callee_member: String,
        arg_index: Option<u32>,
    },
    MakesDecorateCallForOwner {
        member: Option<String>,
    },
    IntrinsicAlias {
        property: String,
    },
}

#[derive(Debug, Clone)]
struct OwnerOrdinalConstraint {
    id: usize,
    owner: SelectorVariableId,
    ordinal: SelectorVariableId,
    kind: OwnerOrdinalConstraintKind,
}

#[derive(Debug, Clone, Copy)]
enum OwnerOrdinalConstraintKind {
    OwnerStatementOrdinal,
}

#[derive(Debug, Clone)]
struct NodeNodeConstraint {
    id: usize,
    left: SelectorVariableId,
    right: SelectorVariableId,
    kind: NodeNodeConstraintKind,
}

#[derive(Debug, Clone, Copy)]
enum NodeNodeConstraintKind {
    AstChild { index: u32 },
}

#[derive(Debug, Clone)]
struct NodeOrdinalConstraint {
    id: usize,
    node: SelectorVariableId,
    ordinal: SelectorVariableId,
    kind: NodeOrdinalConstraintKind,
}

#[derive(Debug, Clone, Copy)]
enum NodeOrdinalConstraintKind {
    AstTopLevel,
}

#[derive(Debug, Clone)]
struct NodeStringConstraint {
    id: usize,
    node: SelectorVariableId,
    string: SelectorVariableId,
    kind: NodeStringConstraintKind,
}

#[derive(Debug, Clone, Copy)]
enum NodeStringConstraintKind {
    AstStringLiteral,
}

fn optional_const_string(value: &Option<StringTerm>) -> Option<Option<String>> {
    match value {
        Some(StringTerm::Const { value }) => Some(Some(value.clone())),
        Some(StringTerm::Var { .. }) => None,
        None => Some(None),
    }
}

fn atom_kind(atom: &SelectorAtom) -> &'static str {
    match atom {
        SelectorAtom::OwnerKind { .. } => "owner_kind",
        SelectorAtom::OwnerStatementOrdinal { .. } => "owner_statement_ordinal",
        SelectorAtom::OwnerDeclaresBinding { .. } => "owner_declares_binding",
        SelectorAtom::OwnerExportName { .. } => "owner_export_name",
        SelectorAtom::OwnerReferencesBinding { .. } => "owner_references_binding",
        SelectorAtom::OwnerReferencesOwner { .. } => "owner_references_owner",
        SelectorAtom::OwnerAliasesOwner { .. } => "owner_aliases_owner",
        SelectorAtom::AstKind { .. } => "ast_kind",
        SelectorAtom::AstChild { .. } => "ast_child",
        SelectorAtom::AstChildCount { .. } => "ast_child_count",
        SelectorAtom::AstStringLiteral { .. } => "ast_string_literal",
        SelectorAtom::AstNumberLiteral { .. } => "ast_number_literal",
        SelectorAtom::AstBoolLiteral { .. } => "ast_bool_literal",
        SelectorAtom::AstIdentifierName { .. } => "ast_identifier_name",
        SelectorAtom::AstPropertyName { .. } => "ast_property_name",
        SelectorAtom::AstOperator { .. } => "ast_operator",
        SelectorAtom::AstRegexLiteral { .. } => "ast_regex_literal",
        SelectorAtom::AstTopLevel { .. } => "ast_top_level",
        SelectorAtom::ReadsMember { .. } => "reads_member",
        SelectorAtom::ReadsMemberOfOwner { .. } => "reads_member_of_owner",
        SelectorAtom::ConsumesModuleMember { .. } => "consumes_module_member",
        SelectorAtom::PassedToCall { .. } => "passed_to_call",
        SelectorAtom::PassedToCallOfOwner { .. } => "passed_to_call_of_owner",
        SelectorAtom::MakesDecorateCall { .. } => "makes_decorate_call",
        SelectorAtom::MakesDecorateCallForOwner { .. } => "makes_decorate_call_for_owner",
        SelectorAtom::IntrinsicAlias { .. } => "intrinsic_alias",
        SelectorAtom::SourceMatchCandidate { .. } => "source_match_candidate",
        SelectorAtom::Equal { .. } => "equal",
        SelectorAtom::NotEqual { .. } => "not_equal",
    }
}

#[derive(Debug, Default)]
struct FactIndex {
    all_owners: BTreeSet<OwnerId>,
    all_nodes: BTreeSet<u32>,
    all_statement_ordinals: BTreeSet<StatementOrdinal>,
    all_strings: BTreeSet<String>,
    owner_facts: Vec<(OwnerId, StatementOrdinal, String)>,
    statement_ordinal_by_owner: BTreeMap<OwnerId, StatementOrdinal>,
    owner_by_statement_ordinal: BTreeMap<StatementOrdinal, OwnerId>,
    declared_bindings: Vec<(OwnerId, String)>,
    declared_exports: Vec<(OwnerId, String)>,
    owner_bindings: BTreeMap<OwnerId, BTreeSet<String>>,
    owner_references_binding: Vec<(OwnerId, String, String)>,
    member_reads: Vec<(OwnerId, String)>,
    member_reads_from: Vec<(OwnerId, String, String)>,
    module_member_uses: Vec<(OwnerId, String, String)>,
    call_arguments: Vec<(String, String, usize)>,
    call_arguments_from: Vec<(String, String, String, usize)>,
    decorate_calls: Vec<(String, String, Option<String>)>,
    intrinsic_aliases: Vec<(String, String)>,
    source_match_candidates: Vec<(String, OwnerId, String)>,
    ast_kinds: Vec<(u32, String)>,
    ast_children: Vec<(u32, u32, u32)>,
    ast_child_counts: Vec<(u32, u32)>,
    ast_string_literals: Vec<(u32, String)>,
    ast_string_wildcards: Vec<(u32, String)>,
    ast_number_literals: Vec<(u32, String)>,
    ast_bool_literals: Vec<(u32, bool)>,
    ast_identifier_names: Vec<(u32, String)>,
    ast_property_names: Vec<(u32, String)>,
    ast_operators: Vec<(u32, String)>,
    ast_regex_literals: Vec<(u32, String, String)>,
    ast_super_classes: Vec<(u32, u32)>,
    ast_top_levels: Vec<(u32, StatementOrdinal)>,
}

impl FactIndex {
    fn from_store(store: &SelectorFactStore) -> Self {
        let mut index = Self::default();
        for fact in &store.facts {
            if let SelectorFact::Owner {
                owner,
                statement_ordinal,
                statement_kind,
                ..
            } = fact
            {
                index
                    .owner_facts
                    .push((*owner, *statement_ordinal, statement_kind.clone()));
                index.all_owners.insert(*owner);
                index
                    .statement_ordinal_by_owner
                    .insert(*owner, *statement_ordinal);
                index
                    .owner_by_statement_ordinal
                    .insert(*statement_ordinal, *owner);
                index.all_statement_ordinals.insert(*statement_ordinal);
            }
        }

        for fact in &store.facts {
            match fact {
                SelectorFact::Owner { .. } => {}
                SelectorFact::DeclaredBinding {
                    owner,
                    binding,
                    export_name,
                    ..
                } => {
                    index.declared_bindings.push((*owner, binding.clone()));
                    index
                        .owner_bindings
                        .entry(*owner)
                        .or_default()
                        .insert(binding.clone());
                    if let Some(export_name) = export_name {
                        index.declared_exports.push((*owner, export_name.clone()));
                    }
                }
                SelectorFact::OwnerReferencesBinding {
                    owner,
                    binding,
                    edge_kind,
                    ..
                } => {
                    index.owner_references_binding.push((
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
                    if let Some(owner) = index.owner_by_statement_ordinal.get(statement_ordinal) {
                        index.member_reads.push((*owner, member.clone()));
                        if let Some(object) = object {
                            index
                                .member_reads_from
                                .push((*owner, object.clone(), member.clone()));
                        }
                    }
                }
                SelectorFact::ModuleMemberUse {
                    statement_ordinal,
                    module,
                    member,
                    ..
                } => {
                    if let Some(owner) = index.owner_by_statement_ordinal.get(statement_ordinal) {
                        index
                            .module_member_uses
                            .push((*owner, module.clone(), member.clone()));
                    }
                }
                SelectorFact::CallArgumentUse {
                    argument,
                    callee_object,
                    callee_member,
                    arg_index,
                    ..
                } => {
                    index.call_arguments.push((
                        argument.clone(),
                        callee_member.clone(),
                        *arg_index,
                    ));
                    if let Some(callee_object) = callee_object {
                        index.call_arguments_from.push((
                            argument.clone(),
                            callee_object.clone(),
                            callee_member.clone(),
                            *arg_index,
                        ));
                    }
                }
                SelectorFact::DecorateCallUse {
                    callee,
                    class_anchor,
                    member,
                    ..
                } => {
                    index.decorate_calls.push((
                        callee.clone(),
                        class_anchor.clone(),
                        member.clone(),
                    ));
                }
                SelectorFact::IntrinsicAliasUse {
                    binding, property, ..
                } => {
                    index
                        .intrinsic_aliases
                        .push((binding.clone(), property.clone()));
                }
                SelectorFact::SourceMatchCandidate {
                    selector_key,
                    statement_ordinal,
                    binding,
                    ..
                } => {
                    if let Some(owner) = index.owner_by_statement_ordinal.get(statement_ordinal) {
                        index.source_match_candidates.push((
                            selector_key.clone(),
                            *owner,
                            binding.clone(),
                        ));
                    }
                }
                SelectorFact::AstKind {
                    node, node_kind, ..
                } => {
                    index.all_nodes.insert(*node);
                    index
                        .ast_kinds
                        .push((*node, node_kind.as_tag().to_string()));
                }
                SelectorFact::AstChild {
                    parent,
                    index: child_index,
                    child,
                    ..
                } => {
                    index.all_nodes.insert(*parent);
                    index.all_nodes.insert(*child);
                    index.ast_children.push((*parent, *child_index, *child));
                }
                SelectorFact::AstStringLiteral { node, value, .. } => {
                    index.all_nodes.insert(*node);
                    index.all_strings.insert(value.clone());
                    index.ast_string_literals.push((*node, value.clone()));
                }
                SelectorFact::AstStringWildcard { node, token, .. } => {
                    index.all_nodes.insert(*node);
                    index.ast_string_wildcards.push((*node, token.clone()));
                }
                SelectorFact::AstNumberLiteral { node, value, .. } => {
                    index.all_nodes.insert(*node);
                    index.ast_number_literals.push((*node, value.clone()));
                }
                SelectorFact::AstBoolLiteral { node, value, .. } => {
                    index.all_nodes.insert(*node);
                    index.ast_bool_literals.push((*node, *value));
                }
                SelectorFact::AstIdentifierName { node, value, .. } => {
                    index.all_nodes.insert(*node);
                    index.ast_identifier_names.push((*node, value.clone()));
                }
                SelectorFact::AstPropertyName { node, value, .. } => {
                    index.all_nodes.insert(*node);
                    index.ast_property_names.push((*node, value.clone()));
                }
                SelectorFact::AstOperator { node, value, .. } => {
                    index.all_nodes.insert(*node);
                    index.ast_operators.push((*node, value.clone()));
                }
                SelectorFact::AstRegexLiteral {
                    node,
                    pattern,
                    flags,
                    ..
                } => {
                    index.all_nodes.insert(*node);
                    index
                        .ast_regex_literals
                        .push((*node, pattern.clone(), flags.clone()));
                }
                SelectorFact::AstSuperClass {
                    class_node,
                    super_class,
                    ..
                } => {
                    index.all_nodes.insert(*class_node);
                    index.all_nodes.insert(*super_class);
                    index.ast_super_classes.push((*class_node, *super_class));
                }
                SelectorFact::AstTopLevel {
                    node,
                    statement_ordinal,
                    ..
                } => {
                    index.all_nodes.insert(*node);
                    index.all_statement_ordinals.insert(*statement_ordinal);
                    index.ast_top_levels.push((*node, *statement_ordinal));
                }
            }
        }
        let mut child_counts: BTreeMap<u32, u32> =
            index.all_nodes.iter().map(|node| (*node, 0)).collect();
        for (parent, _child_index, _child) in &index.ast_children {
            *child_counts.entry(*parent).or_insert(0) += 1;
        }
        index.ast_child_counts = child_counts.into_iter().collect();
        index
    }

    fn single_binding_for_owner(&self, owner: OwnerId) -> Option<&str> {
        let mut bindings = self.owner_bindings.get(&owner)?.iter();
        let binding = bindings.next()?;
        if bindings.next().is_some() {
            return None;
        }
        Some(binding)
    }

    fn source_match_binding_for_owner(&self, selector_key: &str, owner: OwnerId) -> Option<String> {
        let mut matches = self
            .source_match_candidates
            .iter()
            .filter(|(candidate_key, candidate_owner, _binding)| {
                candidate_key == selector_key && *candidate_owner == owner
            })
            .map(|(_key, _owner, binding)| binding.clone());
        let binding = matches.next()?;
        if matches.next().is_some() {
            return None;
        }
        Some(binding)
    }

    fn source_match_bindings_for_owner(&self, selector_key: &str, owner: OwnerId) -> Vec<String> {
        self.source_match_candidates
            .iter()
            .filter(|(candidate_key, candidate_owner, _binding)| {
                candidate_key == selector_key && *candidate_owner == owner
            })
            .map(|(_key, _owner, binding)| binding.clone())
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect()
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SelectorIrSolverError {
    InvalidProgram(SelectorProgramError),
}

impl fmt::Display for SelectorIrSolverError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidProgram(error) => write!(f, "invalid selector IR program: {error}"),
        }
    }
}

impl Error for SelectorIrSolverError {}

#[cfg(test)]
mod tests {
    use super::*;
    use analysis::{AnalysisHints, ChunkId, analyze_chunk, build_owner_graph};
    use selector_ir::{ClaimKind, ClaimOrigin, VariableDomain};
    use selector_ir_lowering::{
        MemberSelectorLoweringContext, MemberSelectorProgramBuilder, lower_member_selector,
    };
    use spec::{
        AnonymousStatementSelector, BindingSelector, BindingSourceKind, CrossRefRelation,
        CrossRefTarget, IntrinsicAliasTarget, MakesDecorateCallTarget, MemberOfModuleTarget,
        MemberSelectorSpec, PassedToCallTarget, ReadsMemberTarget,
    };

    fn fact_store() -> SelectorFactStore {
        SelectorFactStore {
            facts: vec![
                SelectorFact::Owner {
                    chunk_id: ChunkId(0),
                    owner: OwnerId(1),
                    statement_ordinal: StatementOrdinal(1),
                    statement_kind: "fn_decl".to_string(),
                },
                SelectorFact::DeclaredBinding {
                    chunk_id: ChunkId(0),
                    owner: OwnerId(1),
                    binding: "a".to_string(),
                    export_name: None,
                },
                SelectorFact::Owner {
                    chunk_id: ChunkId(0),
                    owner: OwnerId(2),
                    statement_ordinal: StatementOrdinal(2),
                    statement_kind: "class_decl".to_string(),
                },
                SelectorFact::DeclaredBinding {
                    chunk_id: ChunkId(0),
                    owner: OwnerId(2),
                    binding: "b".to_string(),
                    export_name: None,
                },
            ],
        }
    }

    fn lowered_binding(name: &str, kind: Option<BindingSourceKind>) -> SelectorProgram {
        lower_member_selector(
            &MemberSelectorLoweringContext::new(ChunkId(0), "runtime/widgets"),
            "Widget",
            &MemberSelectorSpec::Binding(BindingSelector {
                name: name.to_string(),
                kind,
            }),
        )
        .unwrap()
        .program
    }

    fn binding_selector(name: &str, kind: Option<BindingSourceKind>) -> MemberSelectorSpec {
        MemberSelectorSpec::Binding(BindingSelector {
            name: name.to_string(),
            kind,
        })
    }

    fn owner(owner: usize, ordinal: usize, statement_kind: &str) -> SelectorFact {
        SelectorFact::Owner {
            chunk_id: ChunkId(0),
            owner: OwnerId(owner),
            statement_ordinal: StatementOrdinal(ordinal),
            statement_kind: statement_kind.to_string(),
        }
    }

    fn declared(owner: usize, binding: &str) -> SelectorFact {
        SelectorFact::DeclaredBinding {
            chunk_id: ChunkId(0),
            owner: OwnerId(owner),
            binding: binding.to_string(),
            export_name: None,
        }
    }

    fn owner_for_binding(facts: &SelectorFactStore, binding: &str) -> OwnerId {
        facts
            .facts
            .iter()
            .find_map(|fact| match fact {
                SelectorFact::DeclaredBinding {
                    owner,
                    binding: actual,
                    ..
                } if actual == binding => Some(*owner),
                _ => None,
            })
            .unwrap_or_else(|| panic!("binding {binding} should have an owner"))
    }

    fn statement_ordinal_for_owner(
        facts: &SelectorFactStore,
        target_owner: OwnerId,
    ) -> StatementOrdinal {
        facts
            .facts
            .iter()
            .find_map(|fact| match fact {
                SelectorFact::Owner {
                    owner,
                    statement_ordinal,
                    ..
                } if *owner == target_owner => Some(*statement_ordinal),
                _ => None,
            })
            .unwrap_or_else(|| panic!("owner {target_owner:?} should have an ordinal"))
    }

    fn fact_store_from_analyzed_source(source: &str) -> SelectorFactStore {
        let module = js_ast::with_swc_globals(|| {
            js_ast::parse_js_module_ast("<selector-ir-solver-test>", source).unwrap()
        });
        let analysis = analyze_chunk(&module, &AnalysisHints::default(), None, |_| None);
        let owner_graph = build_owner_graph(&analysis.facts).unwrap();
        let mut facts = SelectorFactStore::default();
        for node in owner_graph.iter_nodes() {
            facts.push(SelectorFact::Owner {
                chunk_id: ChunkId(0),
                owner: node.id,
                statement_ordinal: node.statement_ordinal,
                statement_kind: node.kind.to_string(),
            });
            for binding in &node.declared {
                facts.push(SelectorFact::DeclaredBinding {
                    chunk_id: ChunkId(0),
                    owner: node.id,
                    binding: binding.0.as_str().to_string(),
                    export_name: None,
                });
            }
        }
        for edge in owner_graph.iter_edges() {
            if let Some(binding) = edge.reason.binding() {
                facts.push(SelectorFact::OwnerReferencesBinding {
                    chunk_id: ChunkId(0),
                    owner: edge.from,
                    binding: binding.0.as_str().to_string(),
                    edge_kind: edge.reason.kind().to_string(),
                });
            }
        }
        for call in chunk_facts::decorate_call_uses(&module) {
            facts.push(SelectorFact::DecorateCallUse {
                chunk_id: ChunkId(0),
                callee: call.callee,
                class_anchor: call.class_anchor,
                member: call.member,
            });
        }
        for alias in chunk_facts::intrinsic_alias_uses(&module) {
            facts.push(SelectorFact::IntrinsicAliasUse {
                chunk_id: ChunkId(0),
                binding: alias.binding,
                property: alias.property,
            });
        }
        facts
    }

    #[test]
    fn solves_lowered_binding_selector_uniquely() {
        let program = lowered_binding("a", None);
        let result = solve(&program, &fact_store()).unwrap();

        assert_eq!(
            result.outcome_for(SelectorTargetId(0)),
            Some(&ClaimOutcome::Unique {
                claim: ResolvedClaim {
                    chunk_id: ChunkId(0),
                    owner: OwnerId(1),
                    statement_ordinal: StatementOrdinal(1),
                    binding: Some("a".to_string()),
                    provenance: Vec::new(),
                },
            })
        );
    }

    #[test]
    fn kind_constraint_filters_candidates() {
        let program = lowered_binding("a", Some(BindingSourceKind::ClassDeclaration));
        let result = solve(&program, &fact_store()).unwrap();

        assert_eq!(
            result.outcome_for(SelectorTargetId(0)),
            Some(&ClaimOutcome::NoMatch)
        );
    }

    #[test]
    fn binding_claim_without_projectable_binding_is_no_match() {
        let mut program = SelectorProgram::default();
        let owner_var = program.add_variable(VariableDomain::Owner, Some("@Target".to_string()));
        let target = program.add_target(
            ChunkId(0),
            owner_var,
            "runtime/widgets",
            ClaimKind::Binding {
                export_name: Some("Target".to_string()),
            },
            ClaimOrigin::MemberSelector,
        );
        program.add_atom(SelectorAtom::OwnerKind {
            owner: OwnerTerm::Var { id: owner_var },
            statement_kind: StringTerm::Const {
                value: "var_decl".to_string(),
            },
        });
        let facts = SelectorFactStore {
            facts: vec![owner(1, 1, "var_decl"), declared(1, "a"), declared(1, "b")],
        };

        let result = solve(&program, &facts).unwrap();

        assert_eq!(result.outcome_for(target), Some(&ClaimOutcome::NoMatch));
    }

    #[test]
    fn reports_ambiguity_for_duplicate_binding_fact() {
        let program = lowered_binding("a", None);
        let mut facts = fact_store();
        facts.facts.extend([
            SelectorFact::Owner {
                chunk_id: ChunkId(0),
                owner: OwnerId(3),
                statement_ordinal: StatementOrdinal(3),
                statement_kind: "fn_decl".to_string(),
            },
            SelectorFact::DeclaredBinding {
                chunk_id: ChunkId(0),
                owner: OwnerId(3),
                binding: "a".to_string(),
                export_name: None,
            },
        ]);
        let result = solve(&program, &facts).unwrap();

        assert!(matches!(
            result.outcome_for(SelectorTargetId(0)),
            Some(ClaimOutcome::Ambiguous { candidates }) if candidates.len() == 2
        ));
    }

    #[test]
    fn all_different_reports_duplicate_unique_claims() {
        let mut program = SelectorProgram::default();
        let left = program.add_variable(VariableDomain::Owner, Some("@Left".to_string()));
        let right = program.add_variable(VariableDomain::Owner, Some("@Right".to_string()));
        let left_target = program.add_target(
            ChunkId(0),
            left,
            "runtime/left",
            ClaimKind::Binding {
                export_name: Some("Left".to_string()),
            },
            ClaimOrigin::MemberSelector,
        );
        let right_target = program.add_target(
            ChunkId(0),
            right,
            "runtime/right",
            ClaimKind::Binding {
                export_name: Some("Right".to_string()),
            },
            ClaimOrigin::MemberSelector,
        );
        program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: OwnerTerm::Var { id: left },
            binding: StringTerm::Const {
                value: "a".to_string(),
            },
        });
        program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: OwnerTerm::Var { id: right },
            binding: StringTerm::Const {
                value: "a".to_string(),
            },
        });
        program.require_all_different(vec![left_target, right_target]);

        let result = solve(&program, &fact_store()).unwrap();
        assert_eq!(
            result.outcome_for(left_target),
            Some(&ClaimOutcome::Duplicate {
                owner: OwnerId(1),
                conflicting_targets: vec![left_target, right_target],
            })
        );
        assert_eq!(
            result.outcome_for(right_target),
            result.outcome_for(left_target)
        );
    }

    #[test]
    fn unsupported_atom_fails_closed_for_target() {
        let mut program = lowered_binding("a", None);
        program.add_atom(SelectorAtom::AstTopLevel {
            node: selector_ir::NodeTerm::Const { node: 1 },
            ordinal: selector_ir::OrdinalTerm::Const {
                ordinal: StatementOrdinal(1),
            },
        });

        let result = solve(&program, &fact_store()).unwrap();
        assert!(matches!(
            result.outcome_for(SelectorTargetId(0)),
            Some(ClaimOutcome::Unsupported { message }) if message.contains("ast_top_level")
        ));
    }

    #[test]
    fn ast_atoms_prune_owner_candidates_through_statement_root() {
        let mut program = SelectorProgram::default();
        let target_owner = program.add_variable(VariableDomain::Owner, Some("@Target".to_string()));
        let ordinal = program.add_variable(
            VariableDomain::StatementOrdinal,
            Some("target.ordinal".to_string()),
        );
        let root = program.add_variable(VariableDomain::AstNode, Some("target.root".to_string()));
        let binding_node =
            program.add_variable(VariableDomain::AstNode, Some("target.binding".to_string()));
        let target = program.add_target(
            ChunkId(0),
            target_owner,
            "runtime/target",
            ClaimKind::Binding {
                export_name: Some("Target".to_string()),
            },
            ClaimOrigin::MemberSelector,
        );
        program.add_atom(SelectorAtom::OwnerStatementOrdinal {
            owner: OwnerTerm::Var { id: target_owner },
            ordinal: selector_ir::OrdinalTerm::Var { id: ordinal },
        });
        program.add_atom(SelectorAtom::AstTopLevel {
            node: selector_ir::NodeTerm::Var { id: root },
            ordinal: selector_ir::OrdinalTerm::Var { id: ordinal },
        });
        program.add_atom(SelectorAtom::AstChild {
            parent: selector_ir::NodeTerm::Var { id: root },
            index: 0,
            child: selector_ir::NodeTerm::Var { id: binding_node },
        });
        program.add_atom(SelectorAtom::AstIdentifierName {
            node: selector_ir::NodeTerm::Var { id: binding_node },
            value: StringTerm::Const {
                value: "b".to_string(),
            },
        });
        let facts = SelectorFactStore {
            facts: vec![
                owner(1, 1, "var_decl"),
                declared(1, "a"),
                owner(2, 2, "var_decl"),
                declared(2, "b"),
                SelectorFact::AstTopLevel {
                    chunk_id: ChunkId(0),
                    node: 10,
                    statement_ordinal: StatementOrdinal(1),
                },
                SelectorFact::AstChild {
                    chunk_id: ChunkId(0),
                    parent: 10,
                    index: 0,
                    child: 11,
                },
                SelectorFact::AstIdentifierName {
                    chunk_id: ChunkId(0),
                    node: 11,
                    value: "a".to_string(),
                },
                SelectorFact::AstTopLevel {
                    chunk_id: ChunkId(0),
                    node: 20,
                    statement_ordinal: StatementOrdinal(2),
                },
                SelectorFact::AstChild {
                    chunk_id: ChunkId(0),
                    parent: 20,
                    index: 0,
                    child: 21,
                },
                SelectorFact::AstIdentifierName {
                    chunk_id: ChunkId(0),
                    node: 21,
                    value: "b".to_string(),
                },
            ],
        };

        let result = solve(&program, &facts).unwrap();

        assert_eq!(
            result.outcome_for(target),
            Some(&ClaimOutcome::Unique {
                claim: ResolvedClaim {
                    chunk_id: ChunkId(0),
                    owner: OwnerId(2),
                    statement_ordinal: StatementOrdinal(2),
                    binding: Some("b".to_string()),
                    provenance: Vec::new(),
                },
            })
        );
    }

    #[test]
    fn ast_child_count_prunes_structural_overmatches() {
        let mut program = SelectorProgram::default();
        let target_owner = program.add_variable(VariableDomain::Owner, Some("@Target".to_string()));
        let ordinal = program.add_variable(
            VariableDomain::StatementOrdinal,
            Some("target.ordinal".to_string()),
        );
        let root = program.add_variable(VariableDomain::AstNode, Some("target.root".to_string()));
        let binding_node =
            program.add_variable(VariableDomain::AstNode, Some("target.binding".to_string()));
        let target = program.add_target(
            ChunkId(0),
            target_owner,
            "runtime/target",
            ClaimKind::Binding {
                export_name: Some("Target".to_string()),
            },
            ClaimOrigin::MemberSelector,
        );
        program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: OwnerTerm::Var { id: target_owner },
            binding: StringTerm::Const {
                value: "b".to_string(),
            },
        });
        program.add_atom(SelectorAtom::OwnerStatementOrdinal {
            owner: OwnerTerm::Var { id: target_owner },
            ordinal: selector_ir::OrdinalTerm::Var { id: ordinal },
        });
        program.add_atom(SelectorAtom::AstTopLevel {
            node: selector_ir::NodeTerm::Var { id: root },
            ordinal: selector_ir::OrdinalTerm::Var { id: ordinal },
        });
        program.add_atom(SelectorAtom::AstChildCount {
            node: selector_ir::NodeTerm::Var { id: root },
            count: 1,
        });
        program.add_atom(SelectorAtom::AstChild {
            parent: selector_ir::NodeTerm::Var { id: root },
            index: 0,
            child: selector_ir::NodeTerm::Var { id: binding_node },
        });
        program.add_atom(SelectorAtom::AstIdentifierName {
            node: selector_ir::NodeTerm::Var { id: binding_node },
            value: StringTerm::Const {
                value: "b".to_string(),
            },
        });
        let facts = SelectorFactStore {
            facts: vec![
                owner(1, 1, "var_decl"),
                declared(1, "b"),
                owner(2, 2, "var_decl"),
                declared(2, "b"),
                SelectorFact::AstTopLevel {
                    chunk_id: ChunkId(0),
                    node: 10,
                    statement_ordinal: StatementOrdinal(1),
                },
                SelectorFact::AstChild {
                    chunk_id: ChunkId(0),
                    parent: 10,
                    index: 0,
                    child: 11,
                },
                SelectorFact::AstChild {
                    chunk_id: ChunkId(0),
                    parent: 10,
                    index: 1,
                    child: 12,
                },
                SelectorFact::AstIdentifierName {
                    chunk_id: ChunkId(0),
                    node: 11,
                    value: "b".to_string(),
                },
                SelectorFact::AstTopLevel {
                    chunk_id: ChunkId(0),
                    node: 20,
                    statement_ordinal: StatementOrdinal(2),
                },
                SelectorFact::AstChild {
                    chunk_id: ChunkId(0),
                    parent: 20,
                    index: 0,
                    child: 21,
                },
                SelectorFact::AstIdentifierName {
                    chunk_id: ChunkId(0),
                    node: 21,
                    value: "b".to_string(),
                },
            ],
        };

        let result = solve(&program, &facts).unwrap();

        assert_eq!(
            result.outcome_for(target),
            Some(&ClaimOutcome::Unique {
                claim: ResolvedClaim {
                    chunk_id: ChunkId(0),
                    owner: OwnerId(2),
                    statement_ordinal: StatementOrdinal(2),
                    binding: Some("b".to_string()),
                    provenance: Vec::new(),
                },
            })
        );
    }

    #[test]
    fn shared_string_variable_prunes_inconsistent_wildcard_matches() {
        let mut program = SelectorProgram::default();
        let target_owner = program.add_variable(VariableDomain::Owner, Some("@Target".to_string()));
        let ordinal = program.add_variable(
            VariableDomain::StatementOrdinal,
            Some("target.ordinal".to_string()),
        );
        let root = program.add_variable(VariableDomain::AstNode, Some("target.root".to_string()));
        let left = program.add_variable(VariableDomain::AstNode, Some("target.left".to_string()));
        let right = program.add_variable(VariableDomain::AstNode, Some("target.right".to_string()));
        let wildcard =
            program.add_variable(VariableDomain::String, Some("target.string".to_string()));
        let target = program.add_target(
            ChunkId(0),
            target_owner,
            "runtime/target",
            ClaimKind::Binding {
                export_name: Some("Target".to_string()),
            },
            ClaimOrigin::MemberSelector,
        );
        program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: OwnerTerm::Var { id: target_owner },
            binding: StringTerm::Const {
                value: "b".to_string(),
            },
        });
        program.add_atom(SelectorAtom::OwnerStatementOrdinal {
            owner: OwnerTerm::Var { id: target_owner },
            ordinal: selector_ir::OrdinalTerm::Var { id: ordinal },
        });
        program.add_atom(SelectorAtom::AstTopLevel {
            node: selector_ir::NodeTerm::Var { id: root },
            ordinal: selector_ir::OrdinalTerm::Var { id: ordinal },
        });
        program.add_atom(SelectorAtom::AstChildCount {
            node: selector_ir::NodeTerm::Var { id: root },
            count: 2,
        });
        program.add_atom(SelectorAtom::AstChild {
            parent: selector_ir::NodeTerm::Var { id: root },
            index: 0,
            child: selector_ir::NodeTerm::Var { id: left },
        });
        program.add_atom(SelectorAtom::AstChild {
            parent: selector_ir::NodeTerm::Var { id: root },
            index: 1,
            child: selector_ir::NodeTerm::Var { id: right },
        });
        program.add_atom(SelectorAtom::AstStringLiteral {
            node: selector_ir::NodeTerm::Var { id: left },
            value: StringTerm::Var { id: wildcard },
        });
        program.add_atom(SelectorAtom::AstStringLiteral {
            node: selector_ir::NodeTerm::Var { id: right },
            value: StringTerm::Var { id: wildcard },
        });
        let facts = SelectorFactStore {
            facts: vec![
                owner(1, 1, "var_decl"),
                declared(1, "b"),
                owner(2, 2, "var_decl"),
                declared(2, "b"),
                SelectorFact::AstTopLevel {
                    chunk_id: ChunkId(0),
                    node: 10,
                    statement_ordinal: StatementOrdinal(1),
                },
                SelectorFact::AstChild {
                    chunk_id: ChunkId(0),
                    parent: 10,
                    index: 0,
                    child: 11,
                },
                SelectorFact::AstChild {
                    chunk_id: ChunkId(0),
                    parent: 10,
                    index: 1,
                    child: 12,
                },
                SelectorFact::AstStringLiteral {
                    chunk_id: ChunkId(0),
                    node: 11,
                    value: "x".to_string(),
                },
                SelectorFact::AstStringLiteral {
                    chunk_id: ChunkId(0),
                    node: 12,
                    value: "y".to_string(),
                },
                SelectorFact::AstTopLevel {
                    chunk_id: ChunkId(0),
                    node: 20,
                    statement_ordinal: StatementOrdinal(2),
                },
                SelectorFact::AstChild {
                    chunk_id: ChunkId(0),
                    parent: 20,
                    index: 0,
                    child: 21,
                },
                SelectorFact::AstChild {
                    chunk_id: ChunkId(0),
                    parent: 20,
                    index: 1,
                    child: 22,
                },
                SelectorFact::AstStringLiteral {
                    chunk_id: ChunkId(0),
                    node: 21,
                    value: "z".to_string(),
                },
                SelectorFact::AstStringLiteral {
                    chunk_id: ChunkId(0),
                    node: 22,
                    value: "z".to_string(),
                },
            ],
        };

        let result = solve(&program, &facts).unwrap();

        assert_eq!(
            result.outcome_for(target),
            Some(&ClaimOutcome::Unique {
                claim: ResolvedClaim {
                    chunk_id: ChunkId(0),
                    owner: OwnerId(2),
                    statement_ordinal: StatementOrdinal(2),
                    binding: Some("b".to_string()),
                    provenance: Vec::new(),
                },
            })
        );
    }

    #[test]
    fn solves_cross_ref_anchor_in_one_program() {
        let mut builder = MemberSelectorProgramBuilder::new(MemberSelectorLoweringContext::new(
            ChunkId(0),
            "runtime/widgets",
        ));
        let anchor = builder
            .lower_member_selector("Anchor", &binding_selector("a", None))
            .unwrap();
        let delegator = builder
            .lower_member_selector(
                "Delegator",
                &MemberSelectorSpec::CrossRef(CrossRefTarget {
                    relation: CrossRefRelation::References,
                    anchor: "Anchor".to_string(),
                    kind: Some(BindingSourceKind::FunctionDeclaration),
                }),
            )
            .unwrap();
        let program = builder.into_program().unwrap();
        let facts = SelectorFactStore {
            facts: vec![
                owner(1, 1, "fn_decl"),
                declared(1, "a"),
                owner(2, 2, "fn_decl"),
                declared(2, "b"),
                SelectorFact::OwnerReferencesBinding {
                    chunk_id: ChunkId(0),
                    owner: OwnerId(2),
                    binding: "a".to_string(),
                    edge_kind: "eager_use".to_string(),
                },
            ],
        };

        let result = solve(&program, &facts).unwrap();

        assert!(matches!(
            result.outcome_for(anchor),
            Some(ClaimOutcome::Unique { claim }) if claim.owner == OwnerId(1)
        ));
        assert!(matches!(
            result.outcome_for(delegator),
            Some(ClaimOutcome::Unique { claim }) if claim.owner == OwnerId(2)
        ));
    }

    #[test]
    fn missing_dependent_owner_relation_does_not_poison_anchor_claim() {
        let mut builder = MemberSelectorProgramBuilder::new(MemberSelectorLoweringContext::new(
            ChunkId(0),
            "runtime/widgets",
        ));
        let class = builder
            .lower_member_selector("DecoratedClass", &binding_selector("C", None))
            .unwrap();
        let helper = builder
            .lower_member_selector(
                "decorateClassMember",
                &MemberSelectorSpec::MakesDecorateCall(MakesDecorateCallTarget {
                    class: "DecoratedClass".to_string(),
                    member: None,
                    kind: None,
                }),
            )
            .unwrap();
        let alias = builder
            .lower_member_selector(
                "defineProp",
                &MemberSelectorSpec::IntrinsicAlias(IntrinsicAliasTarget {
                    property: "defineProperty".to_string(),
                    referenced_by: "decorateClassMember".to_string(),
                }),
            )
            .unwrap();
        let program = builder.into_program().unwrap();
        let facts = SelectorFactStore {
            facts: vec![
                owner(1, 1, "class_decl"),
                declared(1, "C"),
                owner(2, 2, "var_decl"),
                declared(2, "d"),
                owner(3, 3, "var_decl"),
                declared(3, "p"),
                SelectorFact::DecorateCallUse {
                    chunk_id: ChunkId(0),
                    callee: "d".to_string(),
                    class_anchor: "C".to_string(),
                    member: None,
                },
                SelectorFact::OwnerReferencesBinding {
                    chunk_id: ChunkId(0),
                    owner: OwnerId(2),
                    binding: "p".to_string(),
                    edge_kind: "eager_use".to_string(),
                },
            ],
        };

        let result = solve(&program, &facts).unwrap();

        assert!(matches!(
            result.outcome_for(class),
            Some(ClaimOutcome::Unique { claim }) if claim.owner == OwnerId(1)
        ));
        assert!(matches!(
            result.outcome_for(helper),
            Some(ClaimOutcome::Unique { claim }) if claim.owner == OwnerId(2)
        ));
        assert_eq!(result.outcome_for(alias), Some(&ClaimOutcome::NoMatch));
    }

    #[test]
    fn solves_object_constrained_reads_member_jointly() {
        let mut builder = MemberSelectorProgramBuilder::new(MemberSelectorLoweringContext::new(
            ChunkId(0),
            "runtime/widgets",
        ));
        let object = builder
            .lower_member_selector("Context", &binding_selector("ctx", None))
            .unwrap();
        let reader = builder
            .lower_member_selector(
                "ReadId",
                &MemberSelectorSpec::ReadsMember(ReadsMemberTarget {
                    member: "id".to_string(),
                    object: Some("Context".to_string()),
                    kind: Some(BindingSourceKind::FunctionDeclaration),
                }),
            )
            .unwrap();
        let program = builder.into_program().unwrap();
        let facts = SelectorFactStore {
            facts: vec![
                owner(1, 1, "var_decl"),
                declared(1, "ctx"),
                owner(2, 2, "fn_decl"),
                declared(2, "readId"),
                SelectorFact::MemberRead {
                    chunk_id: ChunkId(0),
                    statement_ordinal: StatementOrdinal(2),
                    object: Some("ctx".to_string()),
                    member: "id".to_string(),
                },
                owner(3, 3, "fn_decl"),
                declared(3, "other"),
                SelectorFact::MemberRead {
                    chunk_id: ChunkId(0),
                    statement_ordinal: StatementOrdinal(3),
                    object: Some("otherCtx".to_string()),
                    member: "id".to_string(),
                },
            ],
        };

        let result = solve(&program, &facts).unwrap();

        assert!(matches!(
            result.outcome_for(object),
            Some(ClaimOutcome::Unique { claim }) if claim.owner == OwnerId(1)
        ));
        assert!(matches!(
            result.outcome_for(reader),
            Some(ClaimOutcome::Unique { claim }) if claim.owner == OwnerId(2)
        ));
    }

    #[test]
    fn solves_use_site_and_decorate_chain_in_one_program() {
        let mut builder = MemberSelectorProgramBuilder::new(MemberSelectorLoweringContext::new(
            ChunkId(0),
            "runtime/widgets",
        ));
        let class = builder
            .lower_member_selector(
                "WidgetClass",
                &binding_selector("C", Some(BindingSourceKind::ClassDeclaration)),
            )
            .unwrap();
        let registry = builder
            .lower_member_selector("Registry", &binding_selector("reg", None))
            .unwrap();
        let accessor = builder
            .lower_member_selector(
                "Accessor",
                &MemberSelectorSpec::PassedToCall(PassedToCallTarget {
                    callee_member: "register".to_string(),
                    object: Some("Registry".to_string()),
                    arg_index: Some(0),
                    kind: Some(BindingSourceKind::ClassDeclaration),
                }),
            )
            .unwrap();
        let helper = builder
            .lower_member_selector(
                "DecorateHelper",
                &MemberSelectorSpec::MakesDecorateCall(MakesDecorateCallTarget {
                    class: "WidgetClass".to_string(),
                    member: Some("ready".to_string()),
                    kind: Some(BindingSourceKind::VariableDeclarator),
                }),
            )
            .unwrap();
        let alias = builder
            .lower_member_selector(
                "DefinePropertyAlias",
                &MemberSelectorSpec::IntrinsicAlias(IntrinsicAliasTarget {
                    property: "defineProperty".to_string(),
                    referenced_by: "DecorateHelper".to_string(),
                }),
            )
            .unwrap();
        let module_consumer = builder
            .lower_member_selector(
                "ModuleConsumer",
                &MemberSelectorSpec::MemberOfModule(MemberOfModuleTarget {
                    module: "./accessors".to_string(),
                    member: "Widget".to_string(),
                    kind: Some(BindingSourceKind::FunctionDeclaration),
                }),
            )
            .unwrap();
        let program = builder.into_program().unwrap();
        let facts = SelectorFactStore {
            facts: vec![
                owner(1, 1, "class_decl"),
                declared(1, "C"),
                owner(2, 2, "var_decl"),
                declared(2, "reg"),
                owner(3, 3, "class_decl"),
                declared(3, "A"),
                owner(4, 4, "var_decl"),
                declared(4, "d"),
                owner(5, 5, "var_decl"),
                declared(5, "p"),
                owner(6, 6, "fn_decl"),
                declared(6, "consume"),
                SelectorFact::CallArgumentUse {
                    chunk_id: ChunkId(0),
                    argument: "A".to_string(),
                    callee_object: Some("reg".to_string()),
                    callee_member: "register".to_string(),
                    arg_index: 0,
                },
                SelectorFact::DecorateCallUse {
                    chunk_id: ChunkId(0),
                    callee: "d".to_string(),
                    class_anchor: "C".to_string(),
                    member: Some("ready".to_string()),
                },
                SelectorFact::IntrinsicAliasUse {
                    chunk_id: ChunkId(0),
                    binding: "p".to_string(),
                    property: "defineProperty".to_string(),
                },
                SelectorFact::OwnerReferencesBinding {
                    chunk_id: ChunkId(0),
                    owner: OwnerId(4),
                    binding: "p".to_string(),
                    edge_kind: "eager_use".to_string(),
                },
                SelectorFact::ModuleMemberUse {
                    chunk_id: ChunkId(0),
                    statement_ordinal: StatementOrdinal(6),
                    module: "./accessors".to_string(),
                    member: "Widget".to_string(),
                },
            ],
        };

        let result = solve(&program, &facts).unwrap();

        for (target, owner) in [
            (class, OwnerId(1)),
            (registry, OwnerId(2)),
            (accessor, OwnerId(3)),
            (helper, OwnerId(4)),
            (alias, OwnerId(5)),
            (module_consumer, OwnerId(6)),
        ] {
            assert!(
                matches!(
                    result.outcome_for(target),
                    Some(ClaimOutcome::Unique { claim }) if claim.owner == owner
                ),
                "target {target:?} should resolve to {owner:?}: {:#?}",
                result.outcome_for(target)
            );
        }
    }

    #[test]
    fn solves_intrinsic_alias_chain_when_class_anchor_is_source_match_candidate() {
        let mut builder = MemberSelectorProgramBuilder::new(MemberSelectorLoweringContext::new(
            ChunkId(0),
            "features/widget",
        ));
        let define_alias_selector = MemberSelectorSpec::IntrinsicAlias(IntrinsicAliasTarget {
            property: "defineProperty".to_string(),
            referenced_by: "applyDecorators".to_string(),
        });
        let descriptor_alias_selector = MemberSelectorSpec::IntrinsicAlias(IntrinsicAliasTarget {
            property: "getOwnPropertyDescriptor".to_string(),
            referenced_by: "applyDecorators".to_string(),
        });
        let helper_selector = MemberSelectorSpec::MakesDecorateCall(MakesDecorateCallTarget {
            class: "DecoratedClass".to_string(),
            member: None,
            kind: None,
        });
        let mut class_selector = AnonymousStatementSelector::exact(
            "const DecoratedClass = class Widget { STMT_LIST; };",
        );
        class_selector.target_binding = Some("DecoratedClass".to_string());
        let class_member_selector = MemberSelectorSpec::SourceMatch(class_selector.clone());

        let define_alias = builder
            .declare_member_target_in_module(
                "features/widget",
                "definePropertyAlias",
                &define_alias_selector,
            )
            .unwrap();
        let descriptor_alias = builder
            .declare_member_target_in_module(
                "features/widget",
                "descriptorAlias",
                &descriptor_alias_selector,
            )
            .unwrap();
        let helper = builder
            .declare_member_target_in_module("features/widget", "applyDecorators", &helper_selector)
            .unwrap();
        let class = builder
            .declare_member_target_in_module(
                "features/widget",
                "DecoratedClass",
                &class_member_selector,
            )
            .unwrap();
        for (export_name, selector) in [
            ("definePropertyAlias", &define_alias_selector),
            ("descriptorAlias", &descriptor_alias_selector),
            ("applyDecorators", &helper_selector),
            ("DecoratedClass", &class_member_selector),
        ] {
            builder
                .lower_member_constraints_in_module("features/widget", export_name, selector)
                .unwrap();
        }
        let program = builder.into_program().unwrap();
        let class_owner = program.targets[class.0].owner;
        let selector_key = program
            .atoms
            .iter()
            .find_map(|atom| match atom {
                selector_ir::SelectorAtom::SourceMatchCandidate {
                    owner: selector_ir::OwnerTerm::Var { id },
                    selector_key: selector_ir::StringTerm::Const { value },
                } if *id == class_owner => Some(value.clone()),
                _ => None,
            })
            .expect("class source_match should lower to a candidate selector key");
        let facts = SelectorFactStore {
            facts: vec![
                owner(1, 1, "var_decl"),
                declared(1, "defineAlias"),
                owner(2, 2, "var_decl"),
                declared(2, "descriptorAlias"),
                owner(3, 3, "var_decl"),
                declared(3, "decorateHelper"),
                owner(4, 4, "var_decl"),
                declared(4, "targetClass"),
                SelectorFact::IntrinsicAliasUse {
                    chunk_id: ChunkId(0),
                    binding: "defineAlias".to_string(),
                    property: "defineProperty".to_string(),
                },
                SelectorFact::IntrinsicAliasUse {
                    chunk_id: ChunkId(0),
                    binding: "descriptorAlias".to_string(),
                    property: "getOwnPropertyDescriptor".to_string(),
                },
                SelectorFact::OwnerReferencesBinding {
                    chunk_id: ChunkId(0),
                    owner: OwnerId(3),
                    binding: "defineAlias".to_string(),
                    edge_kind: "eager_use".to_string(),
                },
                SelectorFact::OwnerReferencesBinding {
                    chunk_id: ChunkId(0),
                    owner: OwnerId(3),
                    binding: "descriptorAlias".to_string(),
                    edge_kind: "eager_use".to_string(),
                },
                SelectorFact::DecorateCallUse {
                    chunk_id: ChunkId(0),
                    callee: "decorateHelper".to_string(),
                    class_anchor: "targetClass".to_string(),
                    member: None,
                },
                SelectorFact::SourceMatchCandidate {
                    chunk_id: ChunkId(0),
                    selector_key,
                    statement_ordinal: StatementOrdinal(4),
                    binding: "targetClass".to_string(),
                },
            ],
        };

        let result = solve(&program, &facts).unwrap();

        for (target, owner) in [
            (define_alias, OwnerId(1)),
            (descriptor_alias, OwnerId(2)),
            (helper, OwnerId(3)),
            (class, OwnerId(4)),
        ] {
            assert!(
                matches!(
                    result.outcome_for(target),
                    Some(ClaimOutcome::Unique { claim }) if claim.owner == owner
                ),
                "target {target:?} should resolve to {owner:?}: {:#?}",
                result.outcome_for(target)
            );
        }
    }

    #[test]
    fn solves_intrinsic_alias_chain_from_analyzed_decorate_trio() {
        let mut builder = MemberSelectorProgramBuilder::new(MemberSelectorLoweringContext::new(
            ChunkId(0),
            "features/widget",
        ));
        let define_alias_selector = MemberSelectorSpec::IntrinsicAlias(IntrinsicAliasTarget {
            property: "defineProperty".to_string(),
            referenced_by: "applyDecorators".to_string(),
        });
        let descriptor_alias_selector = MemberSelectorSpec::IntrinsicAlias(IntrinsicAliasTarget {
            property: "getOwnPropertyDescriptor".to_string(),
            referenced_by: "applyDecorators".to_string(),
        });
        let helper_selector = MemberSelectorSpec::MakesDecorateCall(MakesDecorateCallTarget {
            class: "DecoratedClass".to_string(),
            member: None,
            kind: None,
        });
        let mut class_selector = AnonymousStatementSelector::exact(
            "const DecoratedClass = class Widget { STMT_LIST; };",
        );
        class_selector.target_binding = Some("DecoratedClass".to_string());
        let class_member_selector = MemberSelectorSpec::SourceMatch(class_selector);

        let define_alias = builder
            .declare_member_target_in_module(
                "features/widget",
                "definePropertyAlias",
                &define_alias_selector,
            )
            .unwrap();
        let descriptor_alias = builder
            .declare_member_target_in_module(
                "features/widget",
                "descriptorAlias",
                &descriptor_alias_selector,
            )
            .unwrap();
        let helper = builder
            .declare_member_target_in_module("features/widget", "applyDecorators", &helper_selector)
            .unwrap();
        let class = builder
            .declare_member_target_in_module(
                "features/widget",
                "DecoratedClass",
                &class_member_selector,
            )
            .unwrap();
        for (export_name, selector) in [
            ("definePropertyAlias", &define_alias_selector),
            ("descriptorAlias", &descriptor_alias_selector),
            ("applyDecorators", &helper_selector),
            ("DecoratedClass", &class_member_selector),
        ] {
            builder
                .lower_member_constraints_in_module("features/widget", export_name, selector)
                .unwrap();
        }
        let program = builder.into_program().unwrap();
        let class_owner_var = program.targets[class.0].owner;
        let selector_key = program
            .atoms
            .iter()
            .find_map(|atom| match atom {
                selector_ir::SelectorAtom::SourceMatchCandidate {
                    owner: selector_ir::OwnerTerm::Var { id },
                    selector_key: selector_ir::StringTerm::Const { value },
                } if *id == class_owner_var => Some(value.clone()),
                _ => None,
            })
            .expect("class source_match should lower to a candidate selector key");

        let mut facts = fact_store_from_analyzed_source(
            r#"
var defineAlias = Object.defineProperty,
  descriptorAlias = Object.getOwnPropertyDescriptor,
  decorateHelper = (decorators, target, key, kind) => {
    const desc = kind ? descriptorAlias(target, key) : target;
    for (const decorator of decorators) decorator(target, key, desc);
    defineAlias(target, key, desc);
  };
const targetClass = class LocalWidget {};
decorateHelper([tag], targetClass.prototype, "greet", 1);
"#,
        );
        let class_owner = owner_for_binding(&facts, "targetClass");
        facts.push(SelectorFact::SourceMatchCandidate {
            chunk_id: ChunkId(0),
            selector_key,
            statement_ordinal: statement_ordinal_for_owner(&facts, class_owner),
            binding: "targetClass".to_string(),
        });

        let result = solve(&program, &facts).unwrap();

        for (target, owner) in [
            (define_alias, owner_for_binding(&facts, "defineAlias")),
            (
                descriptor_alias,
                owner_for_binding(&facts, "descriptorAlias"),
            ),
            (helper, owner_for_binding(&facts, "decorateHelper")),
            (class, class_owner),
        ] {
            assert!(
                matches!(
                    result.outcome_for(target),
                    Some(ClaimOutcome::Unique { claim }) if claim.owner == owner
                ),
                "target {target:?} should resolve to {owner:?}: {:#?}",
                result.outcome_for(target)
            );
        }
    }
}
