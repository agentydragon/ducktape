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
    ClaimOutcome, OwnerTerm, ResolvedClaim, SelectorAtom, SelectorFact, SelectorFactStore,
    SelectorProgram, SelectorProgramError, SelectorTarget, SelectorTargetId, SelectorVariableId,
    SolverClaim, SolverResult, StringTerm, VariableDomain,
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

    relation required_references_owner(usize, usize, usize);
    relation required_aliases_owner(usize, usize, usize);
    relation required_reads_member_of_owner(usize, usize, usize, String);
    relation required_passed_to_call_of_owner(usize, usize, usize, String, Option<u32>);
    relation required_makes_decorate_call_for_owner(usize, usize, usize, Option<String>);
    relation required_intrinsic_alias(usize, usize, usize, String);

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
    ascent.run();

    let unary_supports = group_unary_supports(ascent.constraint_support);
    let binary_edges = group_binary_edges(ascent.binary_constraint_edge);
    let candidates =
        solve_candidate_sets(program, &support, &fact_index, unary_supports, binary_edges);

    let mut claims = Vec::new();
    for target in &program.targets {
        if let Some(reason) = support.unsupported_reason_for_target(target) {
            claims.push(SolverClaim {
                target: target.id,
                outcome: ClaimOutcome::Unsupported { message: reason },
            });
            continue;
        }

        let candidates = candidates.get(&target.owner).cloned().unwrap_or_default();
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

fn solve_candidate_sets(
    program: &SelectorProgram,
    support: &ProgramSupport,
    facts: &FactIndex,
    unary_supports: BTreeMap<usize, BTreeSet<OwnerId>>,
    binary_edges: BTreeMap<usize, BTreeSet<(OwnerId, OwnerId)>>,
) -> BTreeMap<SelectorVariableId, BTreeSet<OwnerId>> {
    let mut candidates = BTreeMap::new();
    for variable in &program.variables {
        if variable.domain == VariableDomain::Owner {
            candidates.insert(variable.id, facts.all_owners.clone());
        }
    }

    for (variable, constraints) in &support.unary_constraints_by_var {
        let mut intersection: Option<BTreeSet<OwnerId>> = None;
        for constraint in constraints {
            let supported = unary_supports.get(constraint).cloned().unwrap_or_default();
            intersection = Some(match intersection {
                Some(existing) => existing.intersection(&supported).copied().collect(),
                None => supported,
            });
        }
        if let Some(intersection) = intersection {
            candidates.insert(*variable, intersection);
        }
    }

    let mut changed = true;
    while changed {
        changed = false;
        for constraint in &support.binary_constraints {
            let edges = binary_edges
                .get(&constraint.id)
                .cloned()
                .unwrap_or_default();
            let left_current = candidates
                .get(&constraint.left)
                .cloned()
                .unwrap_or_default();
            let right_current = candidates
                .get(&constraint.right)
                .cloned()
                .unwrap_or_default();

            let left_allowed: BTreeSet<OwnerId> = edges
                .iter()
                .filter(|(left, right)| {
                    left_current.contains(left) && right_current.contains(right)
                })
                .map(|(left, _right)| *left)
                .collect();

            if intersect_candidate_set(&mut candidates, constraint.left, left_allowed) {
                changed = true;
            }
        }
    }

    candidates
}

fn intersect_candidate_set(
    candidates: &mut BTreeMap<SelectorVariableId, BTreeSet<OwnerId>>,
    variable: SelectorVariableId,
    allowed: BTreeSet<OwnerId>,
) -> bool {
    let current = candidates.entry(variable).or_default();
    let next = current.intersection(&allowed).copied().collect();
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
    Some(ResolvedClaim {
        chunk_id: target.chunk_id,
        owner,
        statement_ordinal,
        binding: support.binding_constraint_for(target.owner).or_else(|| {
            support
                .source_match_selector_for(target.owner)
                .and_then(|selector_key| facts.source_match_binding_for_owner(&selector_key, owner))
                .or_else(|| {
                    facts
                        .single_binding_for_owner(owner)
                        .map(ToString::to_string)
                })
        }),
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
    binary_constraints: Vec<BinaryConstraint>,
    unary_constraints_by_var: BTreeMap<SelectorVariableId, Vec<usize>>,
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
                _ => {}
            }
        }
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
    use analysis::ChunkId;
    use selector_ir::{ClaimKind, ClaimOrigin, VariableDomain};
    use selector_ir_lowering::{
        MemberSelectorLoweringContext, MemberSelectorProgramBuilder, lower_member_selector,
    };
    use spec::{
        BindingSelector, BindingSourceKind, CrossRefRelation, CrossRefTarget, IntrinsicAliasTarget,
        MakesDecorateCallTarget, MemberOfModuleTarget, MemberSelectorSpec, PassedToCallTarget,
        ReadsMemberTarget,
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
}
