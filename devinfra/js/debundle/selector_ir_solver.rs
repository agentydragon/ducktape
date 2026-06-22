//! Ascent-backed solver for the global selector IR.
//!
//! This is the first G3 kernel. It intentionally supports only the IR fragment
//! produced by `selector_ir_lowering` today: a target owner variable constrained
//! by `owner_declares_binding`, optionally narrowed by `owner_kind`. Unsupported
//! atoms become explicit `ClaimOutcome::Unsupported` rows instead of being
//! ignored or approximated.

use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

use analysis::{OwnerId, StatementOrdinal};
use ascent::ascent;
use selector_ir::{
    ClaimOutcome, OwnerTerm, ResolvedClaim, SelectorAtom, SelectorFact, SelectorFactStore,
    SelectorProgram, SelectorProgramError, SelectorTarget, SelectorTargetId, SelectorVariableId,
    SolverClaim, SolverResult, StringTerm,
};

ascent! {
    relation owner_fact(usize, usize, String); // owner, statement ordinal, statement kind
    relation declares(usize, String); // owner declares binding

    relation target_owner_var(usize, usize); // target id, owner variable id
    relation required_binding(usize, String); // owner variable id, binding
    relation required_kind(usize, String); // owner variable id, statement kind

    relation binding_candidate(usize, usize); // target id, owner
    binding_candidate(*target, *owner) <--
        target_owner_var(target, var),
        required_binding(var, binding),
        declares(owner, binding),
        owner_fact(owner, _ordinal, _kind);

    relation kind_candidate(usize, usize); // target id, owner
    kind_candidate(*target, *owner) <--
        binding_candidate(target, owner),
        target_owner_var(target, var),
        required_kind(var, required),
        owner_fact(owner, _ordinal, actual),
        if actual == required;
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
    if !support.global_unsupported.is_empty() {
        return Ok(unsupported_result(
            program,
            support.global_unsupported.join("; "),
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
    for target in &program.targets {
        ascent.target_owner_var.push((target.id.0, target.owner.0));
    }
    for (var, binding) in &support.required_binding {
        ascent.required_binding.push((var.0, binding.clone()));
    }
    for (var, kind) in &support.required_kind {
        ascent.required_kind.push((var.0, kind.clone()));
    }
    ascent.run();

    let binding_candidates = group_candidates(ascent.binding_candidate);
    let kind_candidates = group_candidates(ascent.kind_candidate);

    let mut claims = Vec::new();
    for target in &program.targets {
        if let Some(reason) = support.unsupported_reason_for_target(target) {
            claims.push(SolverClaim {
                target: target.id,
                outcome: ClaimOutcome::Unsupported { message: reason },
            });
            continue;
        }

        let candidates = if support.required_kind.contains_key(&target.owner) {
            kind_candidates.get(&target.id).cloned().unwrap_or_default()
        } else {
            binding_candidates
                .get(&target.id)
                .cloned()
                .unwrap_or_default()
        };
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

fn group_candidates(rows: Vec<(usize, usize)>) -> BTreeMap<SelectorTargetId, BTreeSet<OwnerId>> {
    let mut grouped = BTreeMap::new();
    for (target, owner) in rows {
        grouped
            .entry(SelectorTargetId(target))
            .or_insert_with(BTreeSet::new)
            .insert(OwnerId(owner));
    }
    grouped
}

fn classify_candidates(
    target: &SelectorTarget,
    candidates: BTreeSet<OwnerId>,
    facts: &FactIndex,
    support: &ProgramSupport,
) -> ClaimOutcome {
    let claims: Vec<ResolvedClaim> = candidates
        .into_iter()
        .filter_map(|owner| resolved_claim(target, owner, facts, support))
        .collect();
    match claims.as_slice() {
        [] => ClaimOutcome::NoMatch,
        [claim] => ClaimOutcome::Unique {
            claim: claim.clone(),
        },
        _ => ClaimOutcome::Ambiguous { candidates: claims },
    }
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
        binding: support.required_binding.get(&target.owner).cloned(),
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
    required_binding: BTreeMap<SelectorVariableId, String>,
    required_kind: BTreeMap<SelectorVariableId, String>,
    duplicate_binding_constraints: BTreeMap<SelectorVariableId, Vec<String>>,
    duplicate_kind_constraints: BTreeMap<SelectorVariableId, Vec<String>>,
    global_unsupported: Vec<String>,
}

impl ProgramSupport {
    fn from_program(program: &SelectorProgram) -> Self {
        let mut support = Self::default();
        let mut binding_constraints: BTreeMap<SelectorVariableId, Vec<String>> = BTreeMap::new();
        let mut kind_constraints: BTreeMap<SelectorVariableId, Vec<String>> = BTreeMap::new();

        for atom in &program.atoms {
            match atom {
                SelectorAtom::OwnerDeclaresBinding {
                    owner: OwnerTerm::Var { id },
                    binding: StringTerm::Const { value },
                } => binding_constraints
                    .entry(*id)
                    .or_default()
                    .push(value.clone()),
                SelectorAtom::OwnerKind {
                    owner: OwnerTerm::Var { id },
                    statement_kind: StringTerm::Const { value },
                } => kind_constraints.entry(*id).or_default().push(value.clone()),
                unsupported => support.global_unsupported.push(format!(
                    "unsupported selector atom `{}`",
                    atom_kind(unsupported)
                )),
            }
        }

        for (var, values) in binding_constraints {
            let mut unique: Vec<String> = values.into_iter().collect();
            unique.sort();
            unique.dedup();
            match unique.as_slice() {
                [binding] => {
                    support.required_binding.insert(var, binding.clone());
                }
                _ => {
                    support.duplicate_binding_constraints.insert(var, unique);
                }
            }
        }
        for (var, values) in kind_constraints {
            let mut unique: Vec<String> = values.into_iter().collect();
            unique.sort();
            unique.dedup();
            match unique.as_slice() {
                [kind] => {
                    support.required_kind.insert(var, kind.clone());
                }
                _ => {
                    support.duplicate_kind_constraints.insert(var, unique);
                }
            }
        }
        support
    }

    fn unsupported_reason_for_target(&self, target: &SelectorTarget) -> Option<String> {
        if let Some(bindings) = self.duplicate_binding_constraints.get(&target.owner) {
            return Some(format!(
                "target owner variable has multiple binding constraints: {}",
                bindings.join(", ")
            ));
        }
        if let Some(kinds) = self.duplicate_kind_constraints.get(&target.owner) {
            return Some(format!(
                "target owner variable has multiple kind constraints: {}",
                kinds.join(", ")
            ));
        }
        if !self.required_binding.contains_key(&target.owner) {
            return Some(
                "target owner variable is not constrained by owner_declares_binding".to_string(),
            );
        }
        None
    }
}

fn atom_kind(atom: &SelectorAtom) -> &'static str {
    match atom {
        SelectorAtom::OwnerKind { .. } => "owner_kind",
        SelectorAtom::OwnerStatementOrdinal { .. } => "owner_statement_ordinal",
        SelectorAtom::OwnerDeclaresBinding { .. } => "owner_declares_binding",
        SelectorAtom::OwnerReferencesBinding { .. } => "owner_references_binding",
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
        SelectorAtom::ConsumesModuleMember { .. } => "consumes_module_member",
        SelectorAtom::PassedToCall { .. } => "passed_to_call",
        SelectorAtom::MakesDecorateCall { .. } => "makes_decorate_call",
        SelectorAtom::IntrinsicAlias { .. } => "intrinsic_alias",
        SelectorAtom::Equal { .. } => "equal",
        SelectorAtom::NotEqual { .. } => "not_equal",
    }
}

#[derive(Debug, Default)]
struct FactIndex {
    owner_facts: Vec<(OwnerId, StatementOrdinal, String)>,
    statement_ordinal_by_owner: BTreeMap<OwnerId, StatementOrdinal>,
    declared_bindings: Vec<(OwnerId, String)>,
}

impl FactIndex {
    fn from_store(store: &SelectorFactStore) -> Self {
        let mut index = Self::default();
        for fact in &store.facts {
            match fact {
                SelectorFact::Owner {
                    owner,
                    statement_ordinal,
                    statement_kind,
                    ..
                } => {
                    index
                        .owner_facts
                        .push((*owner, *statement_ordinal, statement_kind.clone()));
                    index
                        .statement_ordinal_by_owner
                        .insert(*owner, *statement_ordinal);
                }
                SelectorFact::DeclaredBinding { owner, binding, .. } => {
                    index.declared_bindings.push((*owner, binding.clone()));
                }
                _ => {}
            }
        }
        index
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
    use selector_ir_lowering::{MemberSelectorLoweringContext, lower_member_selector};
    use spec::{BindingSelector, BindingSourceKind, MemberSelectorSpec};

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
}
