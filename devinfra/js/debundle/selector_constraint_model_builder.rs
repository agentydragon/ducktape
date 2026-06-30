//! Lowering from selector IR plus facts into a compact finite-domain problem.

use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::error::Error;
use std::fmt;
use std::time::Instant;

use analysis::{OwnerId, StatementOrdinal};
use chunk_facts::NodeId;
use regex::Regex;
use selector_constraint_backend::{
    AllDifferentReason, AllowedTupleRowsId, BackendValueId, BinaryConstraintKind,
    CompiledSelectorProblem, CompiledSelectorProblemBuilder, CompiledSelectorProblemError,
    ConstraintValue, ConstraintVariableId, SharedVariableDomainId, TargetBindingProjection,
};
use selector_ir::{
    ClaimKind, NodeTerm, OrdinalTerm, OwnerTerm, SelectorAtom, SelectorFact, SelectorFactStore,
    SelectorProgram, SelectorProgramError, SelectorProjectedValue, SelectorVariableId, StringTerm,
    VariableDomain,
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
    for domain in [
        VariableDomain::Owner,
        VariableDomain::AstNode,
        VariableDomain::String,
        VariableDomain::StatementOrdinal,
    ] {
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
        SelectorAtom::OwnerStatementOrdinal { owner, ordinal } => add_owner_ordinal_allowed_tuples(
            model,
            variables,
            owner,
            ordinal,
            &domains.owner_statement_ordinals,
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
        SelectorAtom::OwnerTopLevelRoot { owner, root } => add_cached_owner_node_allowed_tuples(
            model,
            variables,
            owner,
            root,
            &domains.owner_top_level_roots,
            "owner_top_level_root",
            support_cache,
        ),
        SelectorAtom::AstKind { node, node_kind } => add_cached_node_string_indexed_allowed_tuples(
            model,
            variables,
            node,
            &StringTerm::Const {
                value: node_kind.as_tag().to_string(),
            },
            &domains.ast_kinds_index,
            "ast_kind",
            support_cache,
        ),
        SelectorAtom::AstChild {
            parent,
            index,
            child,
        } => add_cached_ast_child_indexed_allowed_tuples(
            model,
            variables,
            parent,
            *index,
            child,
            domains,
            support_cache,
        ),
        SelectorAtom::AstSuperClass {
            class_node,
            super_class,
        } => add_cached_node_node_allowed_tuples(
            model,
            variables,
            class_node,
            super_class,
            &domains.ast_super_classes,
            "ast_super_class".to_string(),
            support_cache,
        ),
        SelectorAtom::AstChildCount { node, count } => add_cached_node_indexed_allowed_tuples(
            model,
            variables,
            node,
            domains
                .ast_child_counts_by_count
                .get(count)
                .map(Vec::as_slice)
                .unwrap_or(&[]),
            format!("ast_child_count fact {node:?} {count:?}"),
            "ast_child_count",
            *count,
            support_cache,
        ),
        SelectorAtom::AstStringLiteral { node, value } => {
            add_cached_node_string_indexed_allowed_tuples(
                model,
                variables,
                node,
                value,
                &domains.ast_string_literals_index,
                "ast_string_literal",
                support_cache,
            )
        }
        SelectorAtom::AstStringLiteralMatchingRegex { node, pattern } => {
            add_cached_ast_string_literal_matching_regex_allowed_tuples(
                model,
                variables,
                node,
                pattern,
                &domains.ast_string_literals,
                support_cache,
            )
        }
        SelectorAtom::AstNumberLiteral { node, value } => {
            add_cached_node_string_indexed_allowed_tuples(
                model,
                variables,
                node,
                value,
                &domains.ast_number_literals_index,
                "ast_number_literal",
                support_cache,
            )
        }
        SelectorAtom::AstBoolLiteral { node, value } => add_cached_node_indexed_allowed_tuples(
            model,
            variables,
            node,
            domains
                .ast_bool_literals_by_value
                .get(value)
                .map(Vec::as_slice)
                .unwrap_or(&[]),
            format!("ast_bool_literal fact {node:?} {value:?}"),
            "ast_bool_literal",
            if *value { 1 } else { 0 },
            support_cache,
        ),
        SelectorAtom::AstIdentifierName { node, value } => {
            add_cached_node_string_indexed_allowed_tuples(
                model,
                variables,
                node,
                value,
                &domains.ast_identifier_names_index,
                "ast_identifier_name",
                support_cache,
            )
        }
        SelectorAtom::AstPropertyName { node, value } => {
            add_cached_node_string_indexed_allowed_tuples(
                model,
                variables,
                node,
                value,
                &domains.ast_property_names_index,
                "ast_property_name",
                support_cache,
            )
        }
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
            support_cache,
        ),
        SelectorAtom::AstOperator { node, value } => add_cached_node_string_indexed_allowed_tuples(
            model,
            variables,
            node,
            value,
            &domains.ast_operators_index,
            "ast_operator",
            support_cache,
        ),
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
            support_cache,
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
            support_cache,
        ),
    }
}

#[derive(Debug, Default)]
struct EncodedSupportCache {
    owner_unary: BTreeMap<String, SharedVariableDomainId>,
    string_unary: BTreeMap<String, SharedVariableDomainId>,
    node_unary: BTreeMap<String, SharedVariableDomainId>,
    owner_string_binary_by_key: BTreeMap<String, AllowedTupleRowsId>,
    owner_owner_binary: BTreeMap<String, AllowedTupleRowsId>,
    node_node_binary: BTreeMap<String, AllowedTupleRowsId>,
    node_string_binary_by_key: BTreeMap<String, AllowedTupleRowsId>,
    owner_string_unary: BTreeMap<(&'static str, String), SharedVariableDomainId>,
    owner_string_binary: BTreeMap<&'static str, AllowedTupleRowsId>,
    owner_node_binary: BTreeMap<&'static str, AllowedTupleRowsId>,
    node_string_unary: BTreeMap<(&'static str, String), SharedVariableDomainId>,
    node_string_binary: BTreeMap<&'static str, AllowedTupleRowsId>,
    node_unary_u32: BTreeMap<(&'static str, u32), SharedVariableDomainId>,
    ast_child_binary_by_index: BTreeMap<u32, AllowedTupleRowsId>,
    child_list_segment: BTreeMap<ChildListSegmentCacheKey, AllowedTupleRowsId>,
    child_list_index_domains: BTreeMap<ChildListIndexDomainCacheKey, SharedVariableDomainId>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
struct ChildListIndexDomainCacheKey {
    parent: ChildListIndexDomainParent,
    start_index: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum ChildListIndexDomainParent {
    Any,
    Const(NodeId),
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
enum ChildListSegmentCacheKey {
    Generic(ChildListGenericSegmentCacheKey),
    Filtered(ChildListFilteredSegmentCacheKey),
    Repeated(ChildListRepeatedSegmentCacheKey),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
struct ChildListGenericSegmentCacheKey {
    start_index: u32,
    segment_len: usize,
    has_position: bool,
    anchored_left: bool,
    anchored_right: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
struct ChildListRepeatedSegmentCacheKey {
    start_index: u32,
    has_position: bool,
    anchored_left: bool,
    anchored_right: bool,
    parent: ChildListRepeatedTerm,
    segment: Vec<ChildListRepeatedTerm>,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
struct ChildListFilteredSegmentCacheKey {
    start_index: u32,
    has_position: bool,
    anchored_left: bool,
    anchored_right: bool,
    parent: ChildListFilteredTerm,
    segment: Vec<ChildListFilteredTerm>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum ChildListFilteredTerm {
    Const(NodeId),
    Var(usize),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum ChildListRepeatedTerm {
    Var(usize),
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

fn restrict_node_variable_to_candidates(
    model: &mut CompiledSelectorProblemBuilder,
    variable: ConstraintVariableId,
    candidates: impl IntoIterator<Item = NodeId>,
) -> Result<(), CompiledSelectorProblemBuildError> {
    let values = candidates
        .into_iter()
        .map(|node| model.intern_ast_node(node))
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

fn cached_node_domain(
    model: &mut CompiledSelectorProblemBuilder,
    support_cache: &mut EncodedSupportCache,
    key: String,
    nodes: impl IntoIterator<Item = NodeId>,
) -> Result<SharedVariableDomainId, CompiledSelectorProblemBuildError> {
    if let Some(domain_id) = support_cache.node_unary.get(&key) {
        return Ok(*domain_id);
    }
    let values = nodes
        .into_iter()
        .map(|node| model.intern_ast_node(node))
        .collect::<Result<Vec<_>, _>>()?;
    let domain_id = model.intern_shared_sparse_variable_domain(VariableDomain::AstNode, values)?;
    support_cache.node_unary.insert(key, domain_id);
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

fn add_node_string_indexed_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    node: &NodeTerm,
    string: &StringTerm,
    index: &NodeStringIndex,
) -> Result<(), CompiledSelectorProblemBuildError> {
    match (node, string) {
        (NodeTerm::Const { node }, StringTerm::Const { value }) => {
            index.contains(*node, value).then_some(()).ok_or_else(|| {
                CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied {
                    atom: format!("node/string fact {node:?} {value:?}"),
                }
            })
        }
        (NodeTerm::Var { id }, StringTerm::Const { value }) => {
            let variable = model_variable(variables, *id)?;
            restrict_node_variable_to_candidates(
                model,
                variable,
                index
                    .nodes_by_value
                    .get(value)
                    .into_iter()
                    .flatten()
                    .copied(),
            )
        }
        (NodeTerm::Const { node }, StringTerm::Var { id }) => {
            let variable = model_variable(variables, *id)?;
            restrict_string_variable_to_candidates(
                model,
                variable,
                index
                    .values_by_node
                    .get(node)
                    .into_iter()
                    .flatten()
                    .map(String::as_str),
            )
        }
        (NodeTerm::Var { id: node_id }, StringTerm::Var { id: string_id }) => {
            let constraint_variables = [
                model_variable(variables, *node_id)?,
                model_variable(variables, *string_id)?,
            ];
            let tuples = index
                .rows
                .iter()
                .map(|(fact_node, fact_string)| {
                    Ok((
                        model.intern_ast_node(*fact_node)?,
                        model.intern_string(fact_string)?,
                    ))
                })
                .collect::<Result<Vec<_>, CompiledSelectorProblemError>>()?;
            add_encoded_allowed_binary_tuple_set(model, constraint_variables, tuples)
        }
    }
}

fn add_cached_node_string_indexed_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    node: &NodeTerm,
    string: &StringTerm,
    index: &NodeStringIndex,
    relation: &'static str,
    support_cache: &mut EncodedSupportCache,
) -> Result<(), CompiledSelectorProblemBuildError> {
    match (node, string) {
        (NodeTerm::Var { id }, StringTerm::Const { value }) => {
            let variable = model_variable(variables, *id)?;
            let key = (relation, value.clone());
            let domain_id = if let Some(domain_id) = support_cache.node_string_unary.get(&key) {
                *domain_id
            } else {
                let values = index
                    .nodes_by_value
                    .get(value)
                    .into_iter()
                    .flatten()
                    .map(|node| model.intern_ast_node(*node))
                    .collect::<Result<Vec<_>, _>>()?;
                let domain_id =
                    model.intern_shared_sparse_variable_domain(VariableDomain::AstNode, values)?;
                support_cache.node_string_unary.insert(key, domain_id);
                domain_id
            };
            model
                .restrict_variable_to_shared_sparse_domain(variable, domain_id)
                .map_err(Into::into)
        }
        (NodeTerm::Var { id: node_id }, StringTerm::Var { id: string_id }) => {
            let constraint_variables = [
                model_variable(variables, *node_id)?,
                model_variable(variables, *string_id)?,
            ];
            if constraint_variables[0] == constraint_variables[1] {
                return add_node_string_indexed_allowed_tuples(
                    model, variables, node, string, index,
                );
            }
            let row_set = if let Some(row_set) = support_cache.node_string_binary.get(relation) {
                *row_set
            } else {
                let tuples = index
                    .rows
                    .iter()
                    .map(|(fact_node, fact_string)| {
                        Ok((
                            model.intern_ast_node(*fact_node)?,
                            model.intern_string(fact_string)?,
                        ))
                    })
                    .collect::<Result<Vec<_>, CompiledSelectorProblemError>>()?;
                let row_set = model.intern_encoded_allowed_binary_row_set(
                    constraint_variables,
                    [VariableDomain::AstNode, VariableDomain::String],
                    tuples,
                )?;
                support_cache.node_string_binary.insert(relation, row_set);
                row_set
            };
            model
                .add_encoded_allowed_row_set(constraint_variables.to_vec(), row_set)
                .map(|_| ())
                .map_err(Into::into)
        }
        _ => add_node_string_indexed_allowed_tuples(model, variables, node, string, index),
    }
}

fn add_node_indexed_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    node: &NodeTerm,
    candidates: &[NodeId],
    atom: String,
) -> Result<(), CompiledSelectorProblemBuildError> {
    match node {
        NodeTerm::Var { id } => {
            let variable = model_variable(variables, *id)?;
            restrict_node_variable_to_candidates(model, variable, candidates.iter().copied())
        }
        NodeTerm::Const { node } => candidates
            .binary_search(node)
            .is_ok()
            .then_some(())
            .ok_or(CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied { atom }),
    }
}

#[allow(clippy::too_many_arguments)]
fn add_cached_node_indexed_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    node: &NodeTerm,
    candidates: &[NodeId],
    atom: String,
    relation: &'static str,
    value: u32,
    support_cache: &mut EncodedSupportCache,
) -> Result<(), CompiledSelectorProblemBuildError> {
    match node {
        NodeTerm::Var { id } => {
            let variable = model_variable(variables, *id)?;
            let key = (relation, value);
            let domain_id = if let Some(domain_id) = support_cache.node_unary_u32.get(&key) {
                *domain_id
            } else {
                let values = candidates
                    .iter()
                    .copied()
                    .map(|node| model.intern_ast_node(node))
                    .collect::<Result<Vec<_>, _>>()?;
                let domain_id =
                    model.intern_shared_sparse_variable_domain(VariableDomain::AstNode, values)?;
                support_cache.node_unary_u32.insert(key, domain_id);
                domain_id
            };
            model
                .restrict_variable_to_shared_sparse_domain(variable, domain_id)
                .map_err(Into::into)
        }
        NodeTerm::Const { .. } => {
            add_node_indexed_allowed_tuples(model, variables, node, candidates, atom)
        }
    }
}

fn add_ast_child_indexed_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    parent: &NodeTerm,
    child_index: u32,
    child: &NodeTerm,
    domains: &FactDomains,
) -> Result<(), CompiledSelectorProblemBuildError> {
    match (parent, child) {
        (NodeTerm::Const { node: parent }, NodeTerm::Const { node: child }) => domains
            .ast_children_by_parent_index
            .get(&(*parent, child_index))
            .is_some_and(|children| children.binary_search(child).is_ok())
            .then_some(())
            .ok_or_else(
                || CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied {
                    atom: format!("ast_child fact {parent:?} {child_index} {child:?}"),
                },
            ),
        (NodeTerm::Const { node: parent }, NodeTerm::Var { id }) => {
            let variable = model_variable(variables, *id)?;
            restrict_node_variable_to_candidates(
                model,
                variable,
                domains
                    .ast_children_by_parent_index
                    .get(&(*parent, child_index))
                    .into_iter()
                    .flatten()
                    .copied(),
            )
        }
        (NodeTerm::Var { id }, NodeTerm::Const { node: child }) => {
            let variable = model_variable(variables, *id)?;
            restrict_node_variable_to_candidates(
                model,
                variable,
                domains
                    .ast_child_parents_by_child_index
                    .get(&(*child, child_index))
                    .into_iter()
                    .flatten()
                    .copied(),
            )
        }
        (NodeTerm::Var { id: parent_id }, NodeTerm::Var { id: child_id }) => {
            let constraint_variables = [
                model_variable(variables, *parent_id)?,
                model_variable(variables, *child_id)?,
            ];
            let tuples = domains
                .ast_children_by_index
                .get(&child_index)
                .into_iter()
                .flatten()
                .map(|(fact_parent, fact_child)| {
                    Ok((
                        model.intern_ast_node(*fact_parent)?,
                        model.intern_ast_node(*fact_child)?,
                    ))
                })
                .collect::<Result<Vec<_>, CompiledSelectorProblemError>>()?;
            add_encoded_allowed_binary_tuple_set(model, constraint_variables, tuples)
        }
    }
}

fn add_cached_ast_child_indexed_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    parent: &NodeTerm,
    child_index: u32,
    child: &NodeTerm,
    domains: &FactDomains,
    support_cache: &mut EncodedSupportCache,
) -> Result<(), CompiledSelectorProblemBuildError> {
    match (parent, child) {
        (NodeTerm::Var { id: parent_id }, NodeTerm::Var { id: child_id }) => {
            let constraint_variables = [
                model_variable(variables, *parent_id)?,
                model_variable(variables, *child_id)?,
            ];
            if constraint_variables[0] == constraint_variables[1] {
                return add_ast_child_indexed_allowed_tuples(
                    model,
                    variables,
                    parent,
                    child_index,
                    child,
                    domains,
                );
            }
            let row_set =
                if let Some(row_set) = support_cache.ast_child_binary_by_index.get(&child_index) {
                    *row_set
                } else {
                    let tuples = domains
                        .ast_children_by_index
                        .get(&child_index)
                        .into_iter()
                        .flatten()
                        .map(|(fact_parent, fact_child)| {
                            Ok((
                                model.intern_ast_node(*fact_parent)?,
                                model.intern_ast_node(*fact_child)?,
                            ))
                        })
                        .collect::<Result<Vec<_>, CompiledSelectorProblemError>>()?;
                    let row_set = model.intern_encoded_allowed_binary_row_set(
                        constraint_variables,
                        [VariableDomain::AstNode, VariableDomain::AstNode],
                        tuples,
                    )?;
                    support_cache
                        .ast_child_binary_by_index
                        .insert(child_index, row_set);
                    row_set
                };
            model
                .add_encoded_allowed_row_set(constraint_variables.to_vec(), row_set)
                .map(|_| ())
                .map_err(Into::into)
        }
        _ => add_ast_child_indexed_allowed_tuples(
            model,
            variables,
            parent,
            child_index,
            child,
            domains,
        ),
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

fn add_cached_owner_node_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    owner: &OwnerTerm,
    node: &NodeTerm,
    facts: &BTreeSet<(OwnerId, NodeId)>,
    relation: &'static str,
    support_cache: &mut EncodedSupportCache,
) -> Result<(), CompiledSelectorProblemBuildError> {
    match (owner, node) {
        (OwnerTerm::Var { id: owner_id }, NodeTerm::Var { id: node_id }) => {
            let constraint_variables = [
                model_variable(variables, *owner_id)?,
                model_variable(variables, *node_id)?,
            ];
            if constraint_variables[0] == constraint_variables[1] {
                return add_owner_node_allowed_tuples(model, variables, owner, node, facts);
            }
            let row_set = if let Some(row_set) = support_cache.owner_node_binary.get(relation) {
                *row_set
            } else {
                let tuples = facts
                    .iter()
                    .map(|(fact_owner, fact_node)| {
                        Ok((
                            model.intern_owner(*fact_owner)?,
                            model.intern_ast_node(*fact_node)?,
                        ))
                    })
                    .collect::<Result<Vec<_>, CompiledSelectorProblemError>>()?;
                let row_set = model.intern_encoded_allowed_binary_row_set(
                    constraint_variables,
                    [VariableDomain::Owner, VariableDomain::AstNode],
                    tuples,
                )?;
                support_cache.owner_node_binary.insert(relation, row_set);
                row_set
            };
            model
                .add_encoded_allowed_row_set(constraint_variables.to_vec(), row_set)
                .map(|_| ())
                .map_err(Into::into)
        }
        _ => add_owner_node_allowed_tuples(model, variables, owner, node, facts),
    }
}

fn add_cached_node_node_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    left: &NodeTerm,
    right: &NodeTerm,
    facts: &BTreeSet<(NodeId, NodeId)>,
    relation_key: String,
    support_cache: &mut EncodedSupportCache,
) -> Result<(), CompiledSelectorProblemBuildError> {
    match (left, right) {
        (NodeTerm::Const { node: left }, NodeTerm::Const { node: right }) => facts
            .contains(&(*left, *right))
            .then_some(())
            .ok_or_else(
                || CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied {
                    atom: format!("node/node fact {left:?} {right:?}"),
                },
            ),
        (NodeTerm::Var { id }, NodeTerm::Const { node: right }) => {
            let variable = model_variable(variables, *id)?;
            let domain_id = cached_node_domain(
                model,
                support_cache,
                format!("{relation_key}:left-by-right:{right}"),
                facts.iter().filter_map(|(fact_left, fact_right)| {
                    (fact_right == right).then_some(*fact_left)
                }),
            )?;
            model
                .restrict_variable_to_shared_sparse_domain(variable, domain_id)
                .map_err(Into::into)
        }
        (NodeTerm::Const { node: left }, NodeTerm::Var { id }) => {
            let variable = model_variable(variables, *id)?;
            let domain_id = cached_node_domain(
                model,
                support_cache,
                format!("{relation_key}:right-by-left:{left}"),
                facts.iter().filter_map(|(fact_left, fact_right)| {
                    (fact_left == left).then_some(*fact_right)
                }),
            )?;
            model
                .restrict_variable_to_shared_sparse_domain(variable, domain_id)
                .map_err(Into::into)
        }
        (NodeTerm::Var { id: left_id }, NodeTerm::Var { id: right_id }) => {
            let constraint_variables = [
                model_variable(variables, *left_id)?,
                model_variable(variables, *right_id)?,
            ];
            if constraint_variables[0] == constraint_variables[1] {
                let domain_id = cached_node_domain(
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
            let row_set = if let Some(row_set) = support_cache.node_node_binary.get(&relation_key) {
                *row_set
            } else {
                let tuples = facts
                    .iter()
                    .map(|(fact_left, fact_right)| {
                        Ok((
                            model.intern_ast_node(*fact_left)?,
                            model.intern_ast_node(*fact_right)?,
                        ))
                    })
                    .collect::<Result<Vec<_>, CompiledSelectorProblemError>>()?;
                let row_set = model.intern_encoded_allowed_binary_row_set(
                    constraint_variables,
                    [VariableDomain::AstNode, VariableDomain::AstNode],
                    tuples,
                )?;
                support_cache.node_node_binary.insert(relation_key, row_set);
                row_set
            };
            model
                .add_encoded_allowed_row_set(constraint_variables.to_vec(), row_set)
                .map(|_| ())
                .map_err(Into::into)
        }
    }
}

fn add_cached_ast_string_literal_matching_regex_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    node: &NodeTerm,
    pattern: &StringTerm,
    facts: &BTreeSet<(NodeId, String)>,
    support_cache: &mut EncodedSupportCache,
) -> Result<(), CompiledSelectorProblemBuildError> {
    let pattern = required_string_term_const(pattern, "ast_string_literal_matching_regex.pattern")?;
    match node {
        NodeTerm::Const { node } => Regex::new(&pattern)
            .ok()
            .is_some_and(|regex| {
                facts
                    .iter()
                    .any(|(fact_node, value)| fact_node == node && regex.is_match(value))
            })
            .then_some(())
            .ok_or_else(
                || CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied {
                    atom: format!("ast_string_literal_matching_regex fact {node:?} {pattern:?}"),
                },
            ),
        NodeTerm::Var { id } => {
            let variable = model_variable(variables, *id)?;
            let domain_id = cached_node_domain(
                model,
                support_cache,
                format!("ast_string_literal_matching_regex:{pattern}"),
                Regex::new(&pattern)
                    .map(|regex| {
                        facts.iter().filter_map(move |(fact_node, value)| {
                            regex.is_match(value).then_some(*fact_node)
                        })
                    })
                    .into_iter()
                    .flatten(),
            )?;
            model
                .restrict_variable_to_shared_sparse_domain(variable, domain_id)
                .map_err(Into::into)
        }
    }
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
    support_cache: &mut EncodedSupportCache,
) -> Result<(), CompiledSelectorProblemBuildError> {
    let pattern = LoweredChildListPattern::from_terms(terms, variables)?;

    if pattern.segments.iter().all(Vec::is_empty) {
        return Ok(());
    }

    if pattern.segments.len() == 1 {
        return add_child_list_segment_constraint(model, &pattern, 0, None, domains, support_cache);
    }

    let positions =
        add_child_list_segment_position_variables(model, &pattern, domains, support_cache)?;
    for (segment_index, position) in positions.iter().copied().enumerate() {
        add_child_list_segment_constraint(
            model,
            &pattern,
            segment_index,
            Some(position),
            domains,
            support_cache,
        )?;
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
    support_cache: &mut EncodedSupportCache,
) -> Result<Vec<ChildListSegmentPosition>, CompiledSelectorProblemBuildError> {
    let cache_key = child_list_index_domain_cache_key(pattern);
    let index_domain =
        if let Some(domain_id) = support_cache.child_list_index_domains.get(&cache_key) {
            *domain_id
        } else {
            let values = child_list_index_domain_values(pattern, domains);
            let domain_id =
                model.intern_internal_statement_ordinal_shared_sparse_variable_domain(values)?;
            support_cache
                .child_list_index_domains
                .insert(cache_key, domain_id);
            domain_id
        };
    let mut positions = Vec::with_capacity(pattern.segments.len());
    for segment_index in 0..pattern.segments.len() {
        let start = model.add_internal_shared_sparse_variable(
            index_domain,
            Some(format!("ast_child_list.segment{segment_index}.start")),
        )?;
        positions.push(ChildListSegmentPosition { start });
    }
    Ok(positions)
}

fn child_list_index_domain_cache_key(
    pattern: &LoweredChildListPattern,
) -> ChildListIndexDomainCacheKey {
    let parent = match pattern.parent {
        LoweredNodeTerm::Var(_) => ChildListIndexDomainParent::Any,
        LoweredNodeTerm::Const(node) => ChildListIndexDomainParent::Const(node),
    };
    ChildListIndexDomainCacheKey {
        parent,
        start_index: pattern.start_index,
    }
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
    support_cache: &mut EncodedSupportCache,
) -> Result<(), CompiledSelectorProblemBuildError> {
    let segment = &pattern.segments[segment_index];
    let mut constraint_variables = position
        .map(|position| vec![position.start])
        .unwrap_or_default();
    constraint_variables.extend(child_list_constraint_variables(
        std::iter::once(pattern.parent).chain(segment.iter().copied()),
    ));

    if let Some((cache_key, cache_variables)) =
        child_list_segment_cache_entry(pattern, segment_index, position, segment)?
    {
        let row_set = if let Some(row_set) = support_cache.child_list_segment.get(&cache_key) {
            *row_set
        } else {
            let row_set = build_child_list_segment_row_set(
                model,
                &cache_key,
                cache_variables.as_slice(),
                domains,
            )?;
            support_cache.child_list_segment.insert(cache_key, row_set);
            row_set
        };
        return add_cached_child_list_row_set(model, cache_variables, row_set);
    }

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

fn child_list_segment_cache_entry(
    pattern: &LoweredChildListPattern,
    segment_index: usize,
    position: Option<ChildListSegmentPosition>,
    segment: &[LoweredNodeTerm],
) -> Result<
    Option<(ChildListSegmentCacheKey, Vec<ConstraintVariableId>)>,
    CompiledSelectorProblemBuildError,
> {
    let terms = std::iter::once(pattern.parent)
        .chain(segment.iter().copied())
        .collect::<Vec<_>>();
    let has_external_variable = position.is_some()
        || terms
            .iter()
            .any(|term| matches!(term, LoweredNodeTerm::Var(_)));
    if !has_external_variable {
        return Ok(None);
    }

    let has_constant = terms
        .iter()
        .any(|term| matches!(term, LoweredNodeTerm::Const(_)));
    if has_constant {
        let mut variables = position
            .map(|position| vec![position.start])
            .unwrap_or_default();
        let mut variable_columns = variables
            .iter()
            .copied()
            .enumerate()
            .map(|(index, variable)| (variable, index))
            .collect::<BTreeMap<_, _>>();
        let parent =
            child_list_filtered_term(pattern.parent, &mut variables, &mut variable_columns);
        let segment = segment
            .iter()
            .copied()
            .map(|term| child_list_filtered_term(term, &mut variables, &mut variable_columns))
            .collect::<Vec<_>>();
        return Ok(Some((
            ChildListSegmentCacheKey::Filtered(ChildListFilteredSegmentCacheKey {
                start_index: pattern.start_index,
                has_position: position.is_some(),
                anchored_left: segment_index == 0 && pattern.anchored_left,
                anchored_right: segment_index == pattern.segments.len() - 1
                    && pattern.anchored_right,
                parent,
                segment,
            }),
            variables,
        )));
    }

    let mut variables = position
        .map(|position| vec![position.start])
        .unwrap_or_default();
    let mut variable_columns = variables
        .iter()
        .copied()
        .enumerate()
        .map(|(index, variable)| (variable, index))
        .collect::<BTreeMap<_, _>>();
    let parent = child_list_cache_term(pattern.parent, &mut variables, &mut variable_columns);
    let segment = segment
        .iter()
        .copied()
        .map(|term| child_list_cache_term(term, &mut variables, &mut variable_columns))
        .collect::<Vec<_>>();

    let has_repeated_variable = terms.len() + usize::from(position.is_some()) > variables.len();
    if has_repeated_variable {
        return Ok(Some((
            ChildListSegmentCacheKey::Repeated(ChildListRepeatedSegmentCacheKey {
                start_index: pattern.start_index,
                has_position: position.is_some(),
                anchored_left: segment_index == 0 && pattern.anchored_left,
                anchored_right: segment_index == pattern.segments.len() - 1
                    && pattern.anchored_right,
                parent,
                segment,
            }),
            variables,
        )));
    }

    Ok(Some((
        ChildListSegmentCacheKey::Generic(ChildListGenericSegmentCacheKey {
            start_index: pattern.start_index,
            segment_len: segment.len(),
            has_position: position.is_some(),
            anchored_left: segment_index == 0 && pattern.anchored_left,
            anchored_right: segment_index == pattern.segments.len() - 1 && pattern.anchored_right,
        }),
        variables,
    )))
}

fn child_list_filtered_term(
    term: LoweredNodeTerm,
    variables: &mut Vec<ConstraintVariableId>,
    variable_columns: &mut BTreeMap<ConstraintVariableId, usize>,
) -> ChildListFilteredTerm {
    match term {
        LoweredNodeTerm::Const(node) => ChildListFilteredTerm::Const(node),
        LoweredNodeTerm::Var(variable) => {
            let column = if let Some(column) = variable_columns.get(&variable) {
                *column
            } else {
                let column = variables.len();
                variables.push(variable);
                variable_columns.insert(variable, column);
                column
            };
            ChildListFilteredTerm::Var(column)
        }
    }
}

fn child_list_cache_term(
    term: LoweredNodeTerm,
    variables: &mut Vec<ConstraintVariableId>,
    variable_columns: &mut BTreeMap<ConstraintVariableId, usize>,
) -> ChildListRepeatedTerm {
    match term {
        LoweredNodeTerm::Const(_) => {
            unreachable!("constant terms use the generic child-list cache")
        }
        LoweredNodeTerm::Var(variable) => {
            let column = if let Some(column) = variable_columns.get(&variable) {
                *column
            } else {
                let column = variables.len();
                variables.push(variable);
                variable_columns.insert(variable, column);
                column
            };
            ChildListRepeatedTerm::Var(column)
        }
    }
}

fn build_child_list_segment_row_set(
    model: &mut CompiledSelectorProblemBuilder,
    cache_key: &ChildListSegmentCacheKey,
    variables: &[ConstraintVariableId],
    domains: &FactDomains,
) -> Result<AllowedTupleRowsId, CompiledSelectorProblemBuildError> {
    match cache_key {
        ChildListSegmentCacheKey::Generic(cache_key) => {
            build_generic_child_list_segment_row_set(model, cache_key, variables, domains)
        }
        ChildListSegmentCacheKey::Filtered(cache_key) => {
            build_filtered_child_list_segment_row_set(model, cache_key, variables, domains)
        }
        ChildListSegmentCacheKey::Repeated(cache_key) => {
            build_repeated_child_list_segment_row_set(model, cache_key, variables, domains)
        }
    }
}

fn build_generic_child_list_segment_row_set(
    model: &mut CompiledSelectorProblemBuilder,
    cache_key: &ChildListGenericSegmentCacheKey,
    variables: &[ConstraintVariableId],
    domains: &FactDomains,
) -> Result<AllowedTupleRowsId, CompiledSelectorProblemBuildError> {
    debug_assert_eq!(
        variables.len(),
        usize::from(cache_key.has_position) + 1 + cache_key.segment_len
    );
    let mut values = Vec::new();
    for candidate_parent in domains.ast_children_by_parent.keys().copied() {
        let subject_children = child_list_subject_children(
            &domains.ast_children_by_parent,
            candidate_parent,
            cache_key.start_index,
        );
        let Some(latest_start) = subject_children.len().checked_sub(cache_key.segment_len) else {
            continue;
        };
        let mut lo = 0;
        let mut hi = latest_start;
        if cache_key.anchored_left {
            hi = 0;
        }
        if cache_key.anchored_right {
            lo = latest_start;
        }
        if lo > hi {
            continue;
        }

        for start in lo..=hi {
            if cache_key.has_position {
                values.push(BackendValueId(i64::from(subject_children[start].0)));
            }
            values.push(model.intern_ast_node(candidate_parent)?);
            for offset in 0..cache_key.segment_len {
                values.push(model.intern_ast_node(subject_children[start + offset].1)?);
            }
        }
    }
    model
        .intern_flat_encoded_allowed_row_set_for_variables(variables, values)
        .map_err(Into::into)
}

fn build_filtered_child_list_segment_row_set(
    model: &mut CompiledSelectorProblemBuilder,
    cache_key: &ChildListFilteredSegmentCacheKey,
    variables: &[ConstraintVariableId],
    domains: &FactDomains,
) -> Result<AllowedTupleRowsId, CompiledSelectorProblemBuildError> {
    let mut values = Vec::new();
    for candidate_parent in
        filtered_child_list_candidate_parents(cache_key.parent, &domains.ast_children_by_parent)
    {
        let subject_children = child_list_subject_children(
            &domains.ast_children_by_parent,
            candidate_parent,
            cache_key.start_index,
        );
        let Some(latest_start) = subject_children.len().checked_sub(cache_key.segment.len()) else {
            continue;
        };
        let mut lo = 0;
        let mut hi = latest_start;
        if cache_key.anchored_left {
            hi = 0;
        }
        if cache_key.anchored_right {
            lo = latest_start;
        }
        if lo > hi {
            continue;
        }

        for start in lo..=hi {
            let mut row = vec![None; variables.len()];
            if cache_key.has_position {
                row[0] = Some(BackendValueId(i64::from(subject_children[start].0)));
            }
            if !bind_child_list_filtered_term(model, cache_key.parent, candidate_parent, &mut row)?
            {
                continue;
            }
            let mut segment_matches = true;
            for (offset, term) in cache_key.segment.iter().copied().enumerate() {
                if !bind_child_list_filtered_term(
                    model,
                    term,
                    subject_children[start + offset].1,
                    &mut row,
                )? {
                    segment_matches = false;
                    break;
                }
            }
            if !segment_matches {
                continue;
            }
            if row.iter().all(Option::is_some) {
                values.extend(row.into_iter().flatten());
            }
        }
    }
    model
        .intern_flat_encoded_allowed_row_set_for_variables(variables, values)
        .map_err(Into::into)
}

fn build_repeated_child_list_segment_row_set(
    model: &mut CompiledSelectorProblemBuilder,
    cache_key: &ChildListRepeatedSegmentCacheKey,
    variables: &[ConstraintVariableId],
    domains: &FactDomains,
) -> Result<AllowedTupleRowsId, CompiledSelectorProblemBuildError> {
    let mut values = Vec::new();
    for candidate_parent in domains.ast_children_by_parent.keys().copied() {
        let subject_children = child_list_subject_children(
            &domains.ast_children_by_parent,
            candidate_parent,
            cache_key.start_index,
        );
        let Some(latest_start) = subject_children.len().checked_sub(cache_key.segment.len()) else {
            continue;
        };
        let mut lo = 0;
        let mut hi = latest_start;
        if cache_key.anchored_left {
            hi = 0;
        }
        if cache_key.anchored_right {
            lo = latest_start;
        }
        if lo > hi {
            continue;
        }

        for start in lo..=hi {
            let mut row = vec![None; variables.len()];
            if cache_key.has_position {
                row[0] = Some(BackendValueId(i64::from(subject_children[start].0)));
            }
            if !bind_child_list_repeated_term(model, cache_key.parent, candidate_parent, &mut row)?
            {
                continue;
            }
            let mut segment_matches = true;
            for (offset, term) in cache_key.segment.iter().copied().enumerate() {
                if !bind_child_list_repeated_term(
                    model,
                    term,
                    subject_children[start + offset].1,
                    &mut row,
                )? {
                    segment_matches = false;
                    break;
                }
            }
            if !segment_matches {
                continue;
            }
            if let Some(row) = row.into_iter().collect::<Option<Vec<_>>>() {
                values.extend(row);
            }
        }
    }
    model
        .intern_flat_encoded_allowed_row_set_for_variables(variables, values)
        .map_err(Into::into)
}

fn add_cached_child_list_row_set(
    model: &mut CompiledSelectorProblemBuilder,
    variables: Vec<ConstraintVariableId>,
    row_set: AllowedTupleRowsId,
) -> Result<(), CompiledSelectorProblemBuildError> {
    if let [variable] = variables.as_slice() {
        let values = model
            .allowed_tuple_row_set(row_set)
            .map_err(CompiledSelectorProblemBuildError::from)?
            .values()
            .to_vec();
        return model
            .restrict_variable_to_encoded_values(*variable, values)
            .map_err(Into::into);
    }

    model
        .add_encoded_allowed_row_set(variables, row_set)
        .map(|_| ())
        .map_err(Into::into)
}

fn bind_child_list_repeated_term(
    model: &mut CompiledSelectorProblemBuilder,
    term: ChildListRepeatedTerm,
    actual: NodeId,
    row: &mut [Option<BackendValueId>],
) -> Result<bool, CompiledSelectorProblemError> {
    match term {
        ChildListRepeatedTerm::Var(column) => {
            let value = model.intern_ast_node(actual)?;
            let Some(current) = row.get_mut(column) else {
                return Ok(false);
            };
            match current {
                Some(existing) => Ok(*existing == value),
                None => {
                    *current = Some(value);
                    Ok(true)
                }
            }
        }
    }
}

fn bind_child_list_filtered_term(
    model: &mut CompiledSelectorProblemBuilder,
    term: ChildListFilteredTerm,
    actual: NodeId,
    row: &mut [Option<BackendValueId>],
) -> Result<bool, CompiledSelectorProblemError> {
    match term {
        ChildListFilteredTerm::Const(expected) => Ok(expected == actual),
        ChildListFilteredTerm::Var(column) => {
            let value = model.intern_ast_node(actual)?;
            let Some(current) = row.get_mut(column) else {
                return Ok(false);
            };
            match current {
                Some(existing) => Ok(*existing == value),
                None => {
                    *current = Some(value);
                    Ok(true)
                }
            }
        }
    }
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

fn filtered_child_list_candidate_parents(
    parent: ChildListFilteredTerm,
    ast_children_by_parent: &BTreeMap<NodeId, Vec<(u32, NodeId)>>,
) -> Box<dyn Iterator<Item = NodeId> + '_> {
    match parent {
        ChildListFilteredTerm::Const(node) => Box::new(std::iter::once(node)),
        ChildListFilteredTerm::Var(_) => Box::new(ast_children_by_parent.keys().copied()),
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

#[allow(clippy::too_many_arguments)]
fn add_ast_bare_property_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    node: &NodeTerm,
    key: &StringTerm,
    identifier: &StringTerm,
    is_binding: bool,
    facts: &BTreeSet<(NodeId, String, String, bool)>,
    support_cache: &mut EncodedSupportCache,
) -> Result<(), CompiledSelectorProblemBuildError> {
    match (node, key, identifier) {
        (
            NodeTerm::Var { id: node_id },
            StringTerm::Const { value: key },
            StringTerm::Var { id: identifier_id },
        ) => {
            let constraint_variables = [
                model_variable(variables, *node_id)?,
                model_variable(variables, *identifier_id)?,
            ];
            let relation_key = format!("ast_bare_property:{is_binding}:{key}");
            let row_set =
                if let Some(row_set) = support_cache.node_string_binary_by_key.get(&relation_key) {
                    *row_set
                } else {
                    let tuples = facts
                        .iter()
                        .filter(
                            |(_fact_node, fact_key, _fact_identifier, fact_is_binding)| {
                                *fact_is_binding == is_binding && fact_key == key
                            },
                        )
                        .map(
                            |(fact_node, _fact_key, fact_identifier, _fact_is_binding)| {
                                Ok((
                                    model.intern_ast_node(*fact_node)?,
                                    model.intern_string(fact_identifier)?,
                                ))
                            },
                        )
                        .collect::<Result<Vec<_>, CompiledSelectorProblemError>>()?;
                    let row_set = model.intern_encoded_allowed_binary_row_set(
                        constraint_variables,
                        [VariableDomain::AstNode, VariableDomain::String],
                        tuples,
                    )?;
                    support_cache
                        .node_string_binary_by_key
                        .insert(relation_key, row_set);
                    row_set
                };
            return model
                .add_encoded_allowed_row_set(constraint_variables.to_vec(), row_set)
                .map(|_| ())
                .map_err(Into::into);
        }
        (
            NodeTerm::Var { id },
            StringTerm::Const { value: key },
            StringTerm::Const { value: identifier },
        ) => {
            let variable = model_variable(variables, *id)?;
            let domain_id = cached_node_domain(
                model,
                support_cache,
                format!("ast_bare_property:{is_binding}:{key}:{identifier}"),
                facts.iter().filter_map(
                    |(fact_node, fact_key, fact_identifier, fact_is_binding)| {
                        (*fact_is_binding == is_binding
                            && fact_key == key
                            && fact_identifier == identifier)
                            .then_some(*fact_node)
                    },
                ),
            )?;
            return model
                .restrict_variable_to_shared_sparse_domain(variable, domain_id)
                .map_err(Into::into);
        }
        _ => {}
    }

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
    support_cache: &mut EncodedSupportCache,
) -> Result<(), CompiledSelectorProblemBuildError> {
    if let (
        NodeTerm::Var { id },
        StringTerm::Const { value: pattern },
        StringTerm::Const { value: flags },
    ) = (node, pattern, flags)
    {
        let variable = model_variable(variables, *id)?;
        let domain_id = cached_node_domain(
            model,
            support_cache,
            format!("ast_regex_literal:{pattern}:{flags}"),
            facts
                .iter()
                .filter_map(|(fact_node, fact_pattern, fact_flags)| {
                    (fact_pattern == pattern && fact_flags == flags).then_some(*fact_node)
                }),
        )?;
        return model
            .restrict_variable_to_shared_sparse_domain(variable, domain_id)
            .map_err(Into::into);
    }

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
    if let [variable] = variables.as_slice()
        && tuples.iter().all(|tuple| tuple.len() == 1)
    {
        return model
            .restrict_variable_to_encoded_values(
                *variable,
                tuples.into_iter().map(|mut tuple| tuple.remove(0)),
            )
            .map_err(Into::into);
    }
    model
        .add_encoded_allowed_tuples(variables, tuples)
        .map(|_| ())
        .map_err(Into::into)
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
    model
        .add_allowed_tuples(constraint_variables, tuples)
        .map(|_| ())
        .map_err(Into::into)
}

fn projected_value(value: SelectorProjectedValue) -> ConstraintValue {
    match value {
        SelectorProjectedValue::Owner(value) => ConstraintValue::Owner(value),
        SelectorProjectedValue::AstNode(value) => ConstraintValue::AstNode(value),
        SelectorProjectedValue::String(value) => ConstraintValue::String(value),
        SelectorProjectedValue::StatementOrdinal(value) => ConstraintValue::StatementOrdinal(value),
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
struct NodeStringIndex {
    nodes_by_value: BTreeMap<String, Vec<NodeId>>,
    values_by_node: BTreeMap<NodeId, Vec<String>>,
    rows: Vec<(NodeId, String)>,
}

impl NodeStringIndex {
    fn from_facts(facts: &BTreeSet<(NodeId, String)>) -> Self {
        let mut index = Self::default();
        for (node, value) in facts {
            index
                .nodes_by_value
                .entry(value.clone())
                .or_default()
                .push(*node);
            index
                .values_by_node
                .entry(*node)
                .or_default()
                .push(value.clone());
            index.rows.push((*node, value.clone()));
        }
        for nodes in index.nodes_by_value.values_mut() {
            nodes.sort_unstable();
            nodes.dedup();
        }
        for values in index.values_by_node.values_mut() {
            values.sort();
            values.dedup();
        }
        index.rows.sort();
        index.rows.dedup();
        index
    }

    fn contains(&self, node: NodeId, value: &str) -> bool {
        self.values_by_node.get(&node).is_some_and(|values| {
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
    owner_top_level_roots: bool,
    owner_references_binding: bool,
    references_owner: bool,
    aliases_owner: bool,
    ast_child_counts: bool,
    ast_top_level_positions: bool,
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
                SelectorAtom::OwnerTopLevelRoot { .. } => {
                    requirements.owner_top_level_roots = true;
                }
                SelectorAtom::AstChildCount { .. } => {
                    requirements.ast_child_counts = true;
                }
                SelectorAtom::AstTopLevel { .. } => {
                    requirements.ast_top_level_positions = true;
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
                | SelectorAtom::OwnerStatementOrdinal { .. }
                | SelectorAtom::OwnerDeclaresBinding { .. }
                | SelectorAtom::ProjectedAllowedTuples { .. }
                | SelectorAtom::OwnerExportName { .. }
                | SelectorAtom::AstKind { .. }
                | SelectorAtom::AstChild { .. }
                | SelectorAtom::AstSuperClass { .. }
                | SelectorAtom::AstStringLiteral { .. }
                | SelectorAtom::AstStringLiteralMatchingRegex { .. }
                | SelectorAtom::AstNumberLiteral { .. }
                | SelectorAtom::AstBoolLiteral { .. }
                | SelectorAtom::AstIdentifierName { .. }
                | SelectorAtom::AstPropertyName { .. }
                | SelectorAtom::AstBareProperty { .. }
                | SelectorAtom::AstOperator { .. }
                | SelectorAtom::AstRegexLiteral { .. }
                | SelectorAtom::OrdinalOffset { .. }
                | SelectorAtom::OrdinalBefore { .. }
                | SelectorAtom::Equal { .. }
                | SelectorAtom::NotEqual { .. }
                | SelectorAtom::AstChildListPattern { .. } => {}
            }
        }
        requirements
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
    owner_kinds_index: OwnerStringIndex,
    declared_bindings_index: OwnerStringIndex,
    export_names_index: OwnerStringIndex,
    ast_kinds_index: NodeStringIndex,
    ast_string_literals_index: NodeStringIndex,
    ast_number_literals_index: NodeStringIndex,
    ast_identifier_names_index: NodeStringIndex,
    ast_property_names_index: NodeStringIndex,
    ast_operators_index: NodeStringIndex,
    ast_child_counts_by_count: BTreeMap<u32, Vec<NodeId>>,
    ast_bool_literals_by_value: BTreeMap<bool, Vec<NodeId>>,
    ast_children_by_index: BTreeMap<u32, Vec<(NodeId, NodeId)>>,
    ast_children_by_parent_index: BTreeMap<(NodeId, u32), Vec<NodeId>>,
    ast_child_parents_by_child_index: BTreeMap<(NodeId, u32), Vec<NodeId>>,
    relation_supports: RelationSupportCache,
}

impl FactDomains {
    fn from_program_and_facts(program: &SelectorProgram, facts: &SelectorFactStore) -> Self {
        let mut domains = Self::default();
        let requirements = DerivedFactRequirements::from_program(program);
        domains.add_facts(facts);
        domains.finalize_indexes();
        domains.add_derived_facts(&requirements);
        domains.add_program_constants(program);
        domains.build_lookup_indexes();
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

    fn build_lookup_indexes(&mut self) {
        self.owner_kinds_index = OwnerStringIndex::from_facts(&self.owner_kinds);
        self.declared_bindings_index = OwnerStringIndex::from_facts(&self.declared_bindings);
        self.export_names_index = OwnerStringIndex::from_facts(&self.export_names);
        self.ast_kinds_index = NodeStringIndex::from_facts(&self.ast_kinds);
        self.ast_string_literals_index = NodeStringIndex::from_facts(&self.ast_string_literals);
        self.ast_number_literals_index = NodeStringIndex::from_facts(&self.ast_number_literals);
        self.ast_identifier_names_index = NodeStringIndex::from_facts(&self.ast_identifier_names);
        self.ast_property_names_index = NodeStringIndex::from_facts(&self.ast_property_names);
        self.ast_operators_index = NodeStringIndex::from_facts(&self.ast_operators);

        self.ast_child_counts_by_count.clear();
        for (node, count) in &self.ast_child_counts {
            self.ast_child_counts_by_count
                .entry(*count)
                .or_default()
                .push(*node);
        }

        self.ast_bool_literals_by_value.clear();
        for (node, value) in &self.ast_bool_literals {
            self.ast_bool_literals_by_value
                .entry(*value)
                .or_default()
                .push(*node);
        }

        self.ast_children_by_index.clear();
        self.ast_children_by_parent_index.clear();
        self.ast_child_parents_by_child_index.clear();
        for (parent, children) in &self.ast_children_by_parent {
            for (index, child) in children {
                self.ast_children_by_index
                    .entry(*index)
                    .or_default()
                    .push((*parent, *child));
                self.ast_children_by_parent_index
                    .entry((*parent, *index))
                    .or_default()
                    .push(*child);
                self.ast_child_parents_by_child_index
                    .entry((*child, *index))
                    .or_default()
                    .push(*parent);
            }
        }

        for nodes in self.ast_child_counts_by_count.values_mut() {
            nodes.sort_unstable();
            nodes.dedup();
        }
        for nodes in self.ast_bool_literals_by_value.values_mut() {
            nodes.sort_unstable();
            nodes.dedup();
        }
        for pairs in self.ast_children_by_index.values_mut() {
            pairs.sort_unstable();
            pairs.dedup();
        }
        for children in self.ast_children_by_parent_index.values_mut() {
            children.sort_unstable();
            children.dedup();
        }
        for parents in self.ast_child_parents_by_child_index.values_mut() {
            parents.sort_unstable();
            parents.dedup();
        }

        self.relation_supports = RelationSupportCache::from_domains(self);
    }

    fn discard_unneeded_raw_relations(&mut self) {
        self.ast_kinds.clear();
        self.ast_child_counts.clear();
        self.ast_number_literals.clear();
        self.ast_bool_literals.clear();
        self.ast_identifier_names.clear();
        self.ast_property_names.clear();
        self.ast_operators.clear();
        self.ast_top_levels.clear();

        self.raw_owner_references_binding.clear();
        self.raw_member_reads.clear();
        self.raw_module_member_uses.clear();
        self.raw_call_arguments.clear();
        self.decorate_calls.clear();
        self.intrinsic_aliases.clear();
    }

    fn discard_full_domain_source_sets(&mut self) {
        self.owners.clear();
        self.nodes.clear();
        self.strings.clear();
        self.ordinals.clear();
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

        if requirements.ast_top_level_positions {
            let mut top_levels = self.ast_top_levels.iter().copied().collect::<Vec<_>>();
            top_levels.sort_by_key(|(node, ordinal)| (*ordinal, *node));
            for (position, (node, _ordinal)) in top_levels.into_iter().enumerate() {
                let position = StatementOrdinal(position);
                self.add_ordinal(position);
                self.ast_top_level_positions.insert((node, position));
            }
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

        if requirements.ast_child_counts {
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
        }

        if requirements.owner_top_level_roots {
            let mut top_level_nodes_by_ordinal: BTreeMap<StatementOrdinal, Vec<NodeId>> =
                BTreeMap::new();
            for (node, ordinal) in &self.ast_top_levels {
                top_level_nodes_by_ordinal
                    .entry(*ordinal)
                    .or_default()
                    .push(*node);
            }
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

    fn add_projected_value(&mut self, value: &SelectorProjectedValue) {
        match value {
            SelectorProjectedValue::Owner(value) => self.add_owner(*value),
            SelectorProjectedValue::AstNode(value) => self.add_node(*value),
            SelectorProjectedValue::String(value) => self.add_string(value),
            SelectorProjectedValue::StatementOrdinal(value) => self.add_ordinal(*value),
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
            decoded_variable_domain(&model, ConstraintVariableId(0)),
            vec![ast_node(10), ast_node(20)]
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
            decoded_variable_domain(&model, ConstraintVariableId(0)),
            vec![owner(10), owner(20)]
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
            decoded_variable_domain(&model, ConstraintVariableId(0)),
            vec![ast_node(10), ast_node(30)]
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
            satisfying_tuples_for(&model, &[ConstraintVariableId(0)]),
            vec![ast_node(100), ast_node(200)]
                .into_iter()
                .map(|node| vec![node])
                .collect::<Vec<_>>()
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
        let mut support_cache = EncodedSupportCache::default();

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
            &mut support_cache,
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
                .filter(|constraint| model.allowed_tuple_rows(constraint).is_empty())
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
                .filter(|constraint| model.allowed_tuple_rows(constraint).is_empty())
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
            decoded_variable_domain(&model, ConstraintVariableId(1)),
            vec![owner(10)]
        );
        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(2)),
            vec![owner(10)]
        );
        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(3), ConstraintVariableId(9)]).tuples,
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
