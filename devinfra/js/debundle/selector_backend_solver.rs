//! Backend-backed selector solver entry point.
//!
//! This module is the narrow bridge from selector IR/facts to a finite-domain
//! backend. It does not choose assignments itself: it lowers to
//! `SelectorBackendProblem`, calls a `SelectorConstraintBackend`, and decodes
//! the backend's assignment into the existing materializer-facing `SolverResult`.

use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

use analysis::{OwnerId, StatementOrdinal};
use selector_constraint_backend::{
    BackendAssignment, BackendAssignmentCoverage, BackendAssignmentError, BackendSolveResult,
    BackendSolveStatus, SelectorBackendProblem, SelectorBackendProblemError,
    SelectorConstraintBackend,
};
use selector_constraint_model::{ConstraintValue, ConstraintVariableId, TargetBindingProjection};
use selector_constraint_model_builder::{
    SelectorConstraintModelBuildError, build_selector_constraint_model,
};
use selector_ir::{
    ClaimKind, ClaimOutcome, ResolvedClaim, SelectorFact, SelectorFactStore, SelectorProgram,
    SelectorTargetId, SolverClaim, SolverResult,
};

pub fn build_backend_problem(
    program: &SelectorProgram,
    facts: &SelectorFactStore,
) -> Result<SelectorBackendProblem, SelectorBackendProblemBuildError> {
    let model = build_selector_constraint_model(program, facts)
        .map_err(SelectorBackendProblemBuildError::Model)?;
    SelectorBackendProblem::from_model(&model)
        .map_err(SelectorBackendProblemBuildError::BackendProblem)
}

pub fn solve_with_backend<B>(
    program: &SelectorProgram,
    facts: &SelectorFactStore,
    backend: &B,
) -> Result<SolverResult, SelectorBackendSolveError<B::Error>>
where
    B: SelectorConstraintBackend,
{
    let problem =
        build_backend_problem(program, facts).map_err(SelectorBackendSolveError::Build)?;
    let result = backend
        .solve(&problem)
        .map_err(SelectorBackendSolveError::Backend)?;
    decode_backend_result(program, facts, &problem, result)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SelectorBackendProblemBuildError {
    Model(SelectorConstraintModelBuildError),
    BackendProblem(SelectorBackendProblemError),
}

impl fmt::Display for SelectorBackendProblemBuildError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Model(err) => write!(f, "failed to build selector constraint model: {err}"),
            Self::BackendProblem(err) => {
                write!(f, "failed to build selector backend problem: {err}")
            }
        }
    }
}

impl Error for SelectorBackendProblemBuildError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Model(err) => Some(err),
            Self::BackendProblem(err) => Some(err),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SelectorBackendSolveError<E> {
    Build(SelectorBackendProblemBuildError),
    Backend(E),
    Assignment(BackendAssignmentError),
    MissingTargetProjection {
        target: SelectorTargetId,
    },
    MissingAssignmentVariable {
        variable: ConstraintVariableId,
    },
    DecodedAssignmentDomainMismatch {
        variable: ConstraintVariableId,
        expected: &'static str,
        actual: ConstraintValue,
    },
    MissingOwnerFact {
        owner: OwnerId,
    },
    EmptySatisfyingAssignments {
        status: BackendSolveStatus,
    },
    UnsatReturnedAssignments,
}

impl<E: fmt::Display> fmt::Display for SelectorBackendSolveError<E> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Build(err) => write!(f, "{err}"),
            Self::Backend(err) => write!(f, "selector backend failed: {err}"),
            Self::Assignment(err) => {
                write!(f, "selector backend returned invalid assignment: {err}")
            }
            Self::MissingTargetProjection { target } => {
                write!(
                    f,
                    "selector backend problem has no projection for target {target:?}"
                )
            }
            Self::MissingAssignmentVariable { variable } => {
                write!(
                    f,
                    "selector backend assignment has no value for variable {variable:?}"
                )
            }
            Self::DecodedAssignmentDomainMismatch {
                variable,
                expected,
                actual,
            } => write!(
                f,
                "selector backend assignment gave variable {variable:?} a {actual:?} value, expected {expected}"
            ),
            Self::MissingOwnerFact { owner } => {
                write!(
                    f,
                    "selector backend assignment selected owner {owner:?} without an owner fact"
                )
            }
            Self::EmptySatisfyingAssignments { status } => {
                write!(
                    f,
                    "selector backend returned {status:?} with no assignments"
                )
            }
            Self::UnsatReturnedAssignments => {
                write!(
                    f,
                    "selector backend returned unsatisfiable with assignments"
                )
            }
        }
    }
}

impl<E> Error for SelectorBackendSolveError<E>
where
    E: Error + 'static,
{
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Build(err) => Some(err),
            Self::Backend(err) => Some(err),
            Self::Assignment(err) => Some(err),
            Self::MissingTargetProjection { .. }
            | Self::MissingAssignmentVariable { .. }
            | Self::DecodedAssignmentDomainMismatch { .. }
            | Self::MissingOwnerFact { .. }
            | Self::EmptySatisfyingAssignments { .. }
            | Self::UnsatReturnedAssignments => None,
        }
    }
}

fn decode_backend_result<E>(
    program: &SelectorProgram,
    facts: &SelectorFactStore,
    problem: &SelectorBackendProblem,
    result: BackendSolveResult,
) -> Result<SolverResult, SelectorBackendSolveError<E>> {
    match result.status {
        BackendSolveStatus::Unsatisfiable => {
            if !result.assignments.is_empty() {
                return Err(SelectorBackendSolveError::UnsatReturnedAssignments);
            }
            Ok(no_match_result(program))
        }
        BackendSolveStatus::Unknown => Ok(unsupported_result(
            program,
            result
                .diagnostic
                .unwrap_or_else(|| "selector backend returned unknown".to_string()),
        )),
        BackendSolveStatus::Satisfiable | BackendSolveStatus::Ambiguous => {
            if result.assignment_coverage != BackendAssignmentCoverage::TargetSupportComplete {
                return Ok(unsupported_result(
                    program,
                    result.diagnostic.unwrap_or_else(|| {
                        "selector backend returned sample assignments, not complete target support"
                            .to_string()
                    }),
                ));
            }
            if result.assignments.is_empty() {
                return Err(SelectorBackendSolveError::EmptySatisfyingAssignments {
                    status: result.status,
                });
            }
            decode_satisfying_assignments(program, facts, problem, &result.assignments)
        }
    }
}

fn decode_satisfying_assignments<E>(
    program: &SelectorProgram,
    facts: &SelectorFactStore,
    problem: &SelectorBackendProblem,
    assignments: &[BackendAssignment],
) -> Result<SolverResult, SelectorBackendSolveError<E>> {
    let facts = MaterializationFacts::from_store(facts);
    let projections = problem
        .target_projections
        .iter()
        .map(|projection| (projection.target, projection))
        .collect::<BTreeMap<_, _>>();
    let mut claims_by_target: BTreeMap<SelectorTargetId, Vec<ResolvedClaim>> = BTreeMap::new();

    for assignment in assignments {
        let decoded = problem
            .decode_assignment(assignment)
            .map_err(SelectorBackendSolveError::Assignment)?;
        for target in &program.targets {
            let projection = projections
                .get(&target.id)
                .ok_or(SelectorBackendSolveError::MissingTargetProjection { target: target.id })?;
            let owner = assigned_owner(&decoded, projection.owner_variable)?;
            let binding = match &projection.binding_projection {
                Some(TargetBindingProjection::Const(binding)) => Some(binding.clone()),
                Some(TargetBindingProjection::Variable(binding_variable)) => {
                    Some(assigned_string(&decoded, *binding_variable)?)
                }
                None => facts
                    .single_binding_for_owner(owner)
                    .map(ToString::to_string),
            };
            if matches!(
                target.claim,
                ClaimKind::Binding { .. } | ClaimKind::BindingGroupMember { .. }
            ) && binding.is_none()
            {
                continue;
            }
            let statement_ordinal = facts
                .statement_ordinal_by_owner
                .get(&owner)
                .copied()
                .ok_or(SelectorBackendSolveError::MissingOwnerFact { owner })?;
            let claim = ResolvedClaim {
                chunk_id: target.chunk_id,
                owner,
                statement_ordinal,
                binding,
                provenance: Vec::new(),
            };
            let claims = claims_by_target.entry(target.id).or_default();
            if !claims.contains(&claim) {
                claims.push(claim);
            }
        }
    }

    Ok(SolverResult {
        claims: program
            .targets
            .iter()
            .map(|target| SolverClaim {
                target: target.id,
                outcome: claims_to_outcome(claims_by_target.remove(&target.id).unwrap_or_default()),
            })
            .collect(),
    })
}

fn assigned_owner<E>(
    assignment: &BTreeMap<ConstraintVariableId, ConstraintValue>,
    variable: ConstraintVariableId,
) -> Result<OwnerId, SelectorBackendSolveError<E>> {
    match assignment.get(&variable) {
        Some(ConstraintValue::Owner(owner)) => Ok(*owner),
        Some(value) => Err(SelectorBackendSolveError::DecodedAssignmentDomainMismatch {
            variable,
            expected: "owner",
            actual: value.clone(),
        }),
        None => Err(SelectorBackendSolveError::MissingAssignmentVariable { variable }),
    }
}

fn assigned_string<E>(
    assignment: &BTreeMap<ConstraintVariableId, ConstraintValue>,
    variable: ConstraintVariableId,
) -> Result<String, SelectorBackendSolveError<E>> {
    match assignment.get(&variable) {
        Some(ConstraintValue::String(value)) => Ok(value.clone()),
        Some(value) => Err(SelectorBackendSolveError::DecodedAssignmentDomainMismatch {
            variable,
            expected: "string",
            actual: value.clone(),
        }),
        None => Err(SelectorBackendSolveError::MissingAssignmentVariable { variable }),
    }
}

fn claims_to_outcome(claims: Vec<ResolvedClaim>) -> ClaimOutcome {
    match claims.as_slice() {
        [] => ClaimOutcome::NoMatch,
        [claim] => ClaimOutcome::Unique {
            claim: claim.clone(),
        },
        _ => ClaimOutcome::Ambiguous { candidates: claims },
    }
}

fn no_match_result(program: &SelectorProgram) -> SolverResult {
    SolverResult {
        claims: program
            .targets
            .iter()
            .map(|target| SolverClaim {
                target: target.id,
                outcome: ClaimOutcome::NoMatch,
            })
            .collect(),
    }
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

#[derive(Debug, Default)]
struct MaterializationFacts {
    statement_ordinal_by_owner: BTreeMap<OwnerId, StatementOrdinal>,
    bindings_by_owner: BTreeMap<OwnerId, BTreeSet<String>>,
}

impl MaterializationFacts {
    fn from_store(facts: &SelectorFactStore) -> Self {
        let mut index = Self::default();
        for fact in &facts.facts {
            match fact {
                SelectorFact::Owner {
                    owner,
                    statement_ordinal,
                    ..
                } => {
                    index
                        .statement_ordinal_by_owner
                        .insert(*owner, *statement_ordinal);
                }
                SelectorFact::DeclaredBinding { owner, binding, .. } => {
                    index
                        .bindings_by_owner
                        .entry(*owner)
                        .or_default()
                        .insert(binding.clone());
                }
                _ => {}
            }
        }
        index
    }

    fn single_binding_for_owner(&self, owner: OwnerId) -> Option<&str> {
        let mut bindings = self.bindings_by_owner.get(&owner)?.iter();
        let binding = bindings.next()?;
        bindings.next().is_none().then_some(binding.as_str())
    }
}

#[cfg(test)]
mod tests {
    use std::convert::Infallible;

    use analysis::{ChunkId, OwnerId, StatementOrdinal};
    use selector_constraint_backend::{
        BackendAssignment, BackendAssignmentCoverage, BackendSolveResult, BackendSolveStatus,
        BackendValueId, BackendVariableAssignment,
    };
    use selector_constraint_model::ConstraintValue;
    use selector_ir::{ClaimOrigin, OwnerTerm, SelectorAtom, StringTerm, VariableDomain};

    use super::*;

    #[derive(Debug)]
    struct SelectingBackend {
        assignments: Vec<Vec<(ConstraintVariableId, ConstraintValue)>>,
        coverage: BackendAssignmentCoverage,
        status: BackendSolveStatus,
    }

    impl SelectorConstraintBackend for SelectingBackend {
        type Error = Infallible;

        fn solve(
            &self,
            problem: &SelectorBackendProblem,
        ) -> Result<BackendSolveResult, Self::Error> {
            let mut assignments = Vec::new();
            for assignment in &self.assignments {
                assignments.push(BackendAssignment {
                    values: assignment
                        .iter()
                        .map(|(variable, value)| BackendVariableAssignment {
                            variable: *variable,
                            value: backend_value_for(problem, value),
                        })
                        .collect(),
                });
            }
            Ok(BackendSolveResult {
                status: self.status.clone(),
                assignment_coverage: self.coverage,
                assignments,
                diagnostic: None,
            })
        }
    }

    fn backend_value_for(
        problem: &SelectorBackendProblem,
        value: &ConstraintValue,
    ) -> BackendValueId {
        let index = problem
            .value_dictionary
            .iter()
            .position(|candidate| candidate == value)
            .expect("test backend value must be in dictionary");
        BackendValueId(index.try_into().unwrap())
    }

    fn owner(value: usize) -> ConstraintValue {
        ConstraintValue::Owner(OwnerId(value))
    }

    fn string(value: &str) -> ConstraintValue {
        ConstraintValue::String(value.to_string())
    }

    fn owner_fact(owner: OwnerId, ordinal: usize, kind: &str) -> SelectorFact {
        SelectorFact::Owner {
            chunk_id: ChunkId(0),
            owner,
            statement_ordinal: StatementOrdinal(ordinal),
            statement_kind: kind.to_string(),
        }
    }

    fn binding_fact(owner: OwnerId, binding: &str, export_name: &str) -> SelectorFact {
        SelectorFact::DeclaredBinding {
            chunk_id: ChunkId(0),
            owner,
            binding: binding.to_string(),
            export_name: Some(export_name.to_string()),
        }
    }

    fn binding_program() -> (SelectorProgram, SelectorTargetId) {
        let mut program = SelectorProgram::default();
        let owner = program.add_variable(VariableDomain::Owner, Some("owner".to_string()));
        let binding = program.add_variable(VariableDomain::String, Some("binding".to_string()));
        let target = program.add_target(
            ChunkId(0),
            owner,
            "module",
            ClaimKind::Binding {
                export_name: Some("Readable".to_string()),
            },
            ClaimOrigin::Synthetic,
        );
        program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: OwnerTerm::Var { id: owner },
            binding: StringTerm::Var { id: binding },
        });
        program.add_atom(SelectorAtom::OwnerExportName {
            owner: OwnerTerm::Var { id: owner },
            export_name: StringTerm::Const {
                value: "Readable".to_string(),
            },
        });
        (program, target)
    }

    fn const_binding_program() -> (SelectorProgram, SelectorTargetId) {
        let mut program = SelectorProgram::default();
        let owner = program.add_variable(VariableDomain::Owner, Some("owner".to_string()));
        let target = program.add_target(
            ChunkId(0),
            owner,
            "module",
            ClaimKind::Binding {
                export_name: Some("Readable".to_string()),
            },
            ClaimOrigin::Synthetic,
        );
        program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: OwnerTerm::Var { id: owner },
            binding: StringTerm::Const {
                value: "minA".to_string(),
            },
        });
        (program, target)
    }

    fn facts() -> SelectorFactStore {
        let mut facts = SelectorFactStore::default();
        facts.push(owner_fact(OwnerId(1), 10, "function"));
        facts.push(binding_fact(OwnerId(1), "minA", "Readable"));
        facts.push(owner_fact(OwnerId(2), 20, "function"));
        facts.push(binding_fact(OwnerId(2), "minB", "Other"));
        facts
    }

    #[test]
    fn builds_backend_problem_from_selector_program() {
        let (program, target) = binding_program();
        let problem = build_backend_problem(&program, &facts()).unwrap();

        assert_eq!(problem.variables.len(), 2);
        assert_eq!(problem.target_projections[0].target, target);
        assert_eq!(
            problem.target_projections[0].owner_variable,
            ConstraintVariableId(0)
        );
        assert_eq!(
            problem.target_projections[0].binding_projection,
            Some(TargetBindingProjection::Variable(ConstraintVariableId(1)))
        );
        assert!(
            problem
                .value_dictionary
                .contains(&ConstraintValue::Owner(OwnerId(1)))
        );
        assert!(
            problem
                .value_dictionary
                .contains(&ConstraintValue::String("minA".to_string()))
        );
    }

    #[test]
    fn backend_problem_preserves_constant_binding_projection() {
        let (program, target) = const_binding_program();
        let problem = build_backend_problem(&program, &facts()).unwrap();

        assert_eq!(problem.target_projections[0].target, target);
        assert_eq!(
            problem.target_projections[0].binding_projection,
            Some(TargetBindingProjection::Const("minA".to_string()))
        );
    }

    #[test]
    fn backend_assignment_decodes_to_solver_result() {
        let (program, target) = binding_program();
        let backend = SelectingBackend {
            status: BackendSolveStatus::Satisfiable,
            coverage: BackendAssignmentCoverage::TargetSupportComplete,
            assignments: vec![vec![
                (ConstraintVariableId(0), owner(1)),
                (ConstraintVariableId(1), string("minA")),
            ]],
        };

        let result = solve_with_backend(&program, &facts(), &backend).unwrap();

        assert_eq!(
            result.outcome_for(target),
            Some(&ClaimOutcome::Unique {
                claim: ResolvedClaim {
                    chunk_id: ChunkId(0),
                    owner: OwnerId(1),
                    statement_ordinal: StatementOrdinal(10),
                    binding: Some("minA".to_string()),
                    provenance: Vec::new(),
                }
            })
        );
    }

    #[test]
    fn constant_binding_projection_decodes_without_binding_variable() {
        let (program, target) = const_binding_program();
        let backend = SelectingBackend {
            status: BackendSolveStatus::Satisfiable,
            coverage: BackendAssignmentCoverage::TargetSupportComplete,
            assignments: vec![vec![(ConstraintVariableId(0), owner(1))]],
        };

        let result = solve_with_backend(&program, &facts(), &backend).unwrap();

        assert_eq!(
            result.outcome_for(target),
            Some(&ClaimOutcome::Unique {
                claim: ResolvedClaim {
                    chunk_id: ChunkId(0),
                    owner: OwnerId(1),
                    statement_ordinal: StatementOrdinal(10),
                    binding: Some("minA".to_string()),
                    provenance: Vec::new(),
                }
            })
        );
    }

    #[test]
    fn multiple_backend_assignments_become_ambiguous_claims() {
        let (program, target) = binding_program();
        let backend = SelectingBackend {
            status: BackendSolveStatus::Ambiguous,
            coverage: BackendAssignmentCoverage::TargetSupportComplete,
            assignments: vec![
                vec![
                    (ConstraintVariableId(0), owner(1)),
                    (ConstraintVariableId(1), string("minA")),
                ],
                vec![
                    (ConstraintVariableId(0), owner(2)),
                    (ConstraintVariableId(1), string("minB")),
                ],
            ],
        };

        let result = solve_with_backend(&program, &facts(), &backend).unwrap();

        match result.outcome_for(target) {
            Some(ClaimOutcome::Ambiguous { candidates }) => {
                assert_eq!(candidates.len(), 2);
                assert!(candidates.iter().any(|claim| claim.owner == OwnerId(1)));
                assert!(candidates.iter().any(|claim| claim.owner == OwnerId(2)));
            }
            other => panic!("expected ambiguous backend result, got {other:?}"),
        }
    }

    #[test]
    fn unsat_backend_result_maps_to_no_match() {
        let (program, target) = binding_program();
        let backend = SelectingBackend {
            status: BackendSolveStatus::Unsatisfiable,
            coverage: BackendAssignmentCoverage::Sample,
            assignments: Vec::new(),
        };

        let result = solve_with_backend(&program, &facts(), &backend).unwrap();

        assert_eq!(result.outcome_for(target), Some(&ClaimOutcome::NoMatch));
    }

    #[test]
    fn sample_backend_assignment_maps_to_unsupported_not_unique() {
        let (program, target) = binding_program();
        let backend = SelectingBackend {
            status: BackendSolveStatus::Satisfiable,
            coverage: BackendAssignmentCoverage::Sample,
            assignments: vec![vec![
                (ConstraintVariableId(0), owner(1)),
                (ConstraintVariableId(1), string("minA")),
            ]],
        };

        let result = solve_with_backend(&program, &facts(), &backend).unwrap();

        match result.outcome_for(target) {
            Some(ClaimOutcome::Unsupported { message }) => {
                assert!(message.contains("sample assignments"));
            }
            other => panic!("expected unsupported sample backend result, got {other:?}"),
        }
    }
}
