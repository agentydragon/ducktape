//! Backend-backed selector solver entry point.
//!
//! This module is the narrow bridge from selector IR/facts to a finite-domain
//! backend. It does not choose assignments itself: it lowers to a compact
//! compiled problem, calls a backend, and decodes the backend's assignment into
//! the existing materializer-facing `SolverResult`.

use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

use analysis::{OwnerId, StatementOrdinal};
use selector_constraint_backend::{
    BackendAssignment, BackendAssignmentCoverage, BackendAssignmentError, BackendSolveResult,
    BackendSolveStatus, BackendVariableAssignment, CompiledSelectorProblem, ConstraintValue,
    ConstraintVariableId, MAX_ALTERNATIVES_PER_VARIABLE, SelectorProblemBackend,
    TargetBindingProjection,
};
use selector_constraint_model_builder::{
    CompiledSelectorProblemBuildError, compile_selector_problem,
};
use selector_ir::{
    ClaimKind, ClaimOutcome, ResolvedClaim, SelectorFact, SelectorFactStore, SelectorProgram,
    SelectorTargetId, SolverClaim, SolverResult,
};

/// What every target of a program that admits no assignment reports.
const UNSATISFIABLE_REASON: &str = "selectors that interact with this one admit no joint \
    assignment: two or more claim the same place or contradict a relation, and which ones is \
    not determined";

pub fn solve_with_backend<B>(
    program: &SelectorProgram,
    facts: &SelectorFactStore,
    backend: &B,
) -> Result<SolverResult, SelectorBackendSolveError<B::Error>>
where
    B: SelectorProblemBackend,
{
    let problem =
        compile_selector_problem(program, facts).map_err(SelectorBackendSolveError::Build)?;
    if problem.known_unsat {
        return Ok(unsatisfiable_result(program));
    }
    if let Some(result) = singleton_no_constraint_backend_result(&problem) {
        return decode_backend_result(program, facts, &problem, result);
    }
    let result = backend
        .solve(&problem)
        .map_err(SelectorBackendSolveError::Backend)?;
    decode_backend_result(program, facts, &problem, result)
}

fn singleton_no_constraint_backend_result(
    problem: &CompiledSelectorProblem,
) -> Option<BackendSolveResult> {
    // This is not a second exact-assignment backend. It is only the terminal case
    // where model compilation/simplification has already proven that no
    // constraints remain and every variable has exactly one possible value.
    if problem.known_unsat
        || !problem.allowed_tuples.is_empty()
        || !problem.all_different.is_empty()
    {
        return None;
    }

    if problem
        .variables
        .iter()
        .any(|variable| problem.variable_domain_value_count(variable) != 1)
    {
        return None;
    }

    let mut singleton_values = BTreeMap::new();
    for variable in &problem.variables {
        let domain_values = problem.variable_domain_values(variable);
        let [value] = domain_values.as_slice() else {
            unreachable!()
        };
        singleton_values.insert(variable.id, *value);
    }

    let mut projected_variables = BTreeSet::new();
    for projection in &problem.target_projections {
        projected_variables.insert(projection.owner_variable);
        if let Some(TargetBindingProjection::Variable(variable)) =
            projection.binding_projection.as_ref()
        {
            projected_variables.insert(*variable);
        }
    }
    // The synthetic backend assignment is intentionally partial. The decoder
    // consumes only target owner variables plus optional target binding variables.
    let values = projected_variables
        .into_iter()
        .map(|variable| {
            Some(BackendVariableAssignment {
                variable,
                value: *singleton_values.get(&variable)?,
            })
        })
        .collect::<Option<Vec<_>>>()?;

    Some(BackendSolveResult {
        status: BackendSolveStatus::Satisfiable,
        assignment_coverage: BackendAssignmentCoverage::TargetSupportComplete,
        assignments: vec![BackendAssignment { values }],
        diagnostic: None,
        fixed_variables: BTreeSet::new(),
    })
}

#[derive(Debug)]
pub enum SelectorBackendSolveError<E> {
    Build(CompiledSelectorProblemBuildError),
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
    /// A satisfiable or ambiguous status whose assignments are only a
    /// sample, so they prove neither uniqueness nor the alternatives.
    SampleCoverage {
        status: BackendSolveStatus,
    },
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
            Self::SampleCoverage { status } => {
                write!(
                    f,
                    "selector backend returned {status:?} with sample assignments, not complete \
                     target support"
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
            | Self::UnsatReturnedAssignments
            | Self::SampleCoverage { .. } => None,
        }
    }
}

fn decode_backend_result<E>(
    program: &SelectorProgram,
    facts: &SelectorFactStore,
    problem: &CompiledSelectorProblem,
    result: BackendSolveResult,
) -> Result<SolverResult, SelectorBackendSolveError<E>> {
    match result.status {
        BackendSolveStatus::Unsatisfiable => {
            if !result.assignments.is_empty() {
                return Err(SelectorBackendSolveError::UnsatReturnedAssignments);
            }
            Ok(unsatisfiable_result(program))
        }
        BackendSolveStatus::Unknown => decode_unknown_result(program, facts, problem, result),
        BackendSolveStatus::Satisfiable | BackendSolveStatus::Ambiguous => {
            let capped = match result.assignment_coverage {
                BackendAssignmentCoverage::TargetSupportComplete => false,
                BackendAssignmentCoverage::TargetSupportCapped => true,
                BackendAssignmentCoverage::Sample => {
                    return Err(SelectorBackendSolveError::SampleCoverage {
                        status: result.status,
                    });
                }
            };
            if result.assignments.is_empty() {
                return Err(SelectorBackendSolveError::EmptySatisfyingAssignments {
                    status: result.status,
                });
            }
            decode_satisfying_assignments(
                program,
                facts,
                problem,
                &result.assignments,
                capped,
                &BTreeMap::new(),
            )
        }
    }
}

/// The backend stopped before finishing. A target is resolved only when every
/// variable it projects was proven fixed before the stop; every other target
/// is undecided.
fn decode_unknown_result<E>(
    program: &SelectorProgram,
    facts: &SelectorFactStore,
    problem: &CompiledSelectorProblem,
    result: BackendSolveResult,
) -> Result<SolverResult, SelectorBackendSolveError<E>> {
    if result.assignments.is_empty() && !result.fixed_variables.is_empty() {
        return Err(SelectorBackendSolveError::EmptySatisfyingAssignments {
            status: result.status,
        });
    }
    let reason = result
        .diagnostic
        .unwrap_or_else(|| "the selector backend stopped before deciding".to_string());
    let mut undecided = BTreeMap::new();
    for projection in &problem.target_projections {
        let fixed = result.fixed_variables.contains(&projection.owner_variable)
            && match &projection.binding_projection {
                Some(TargetBindingProjection::Variable(binding)) => {
                    result.fixed_variables.contains(binding)
                }
                Some(TargetBindingProjection::Const(_)) | None => true,
            };
        if !fixed {
            undecided.insert(
                projection.target,
                ClaimOutcome::Undecided {
                    reason: reason.clone(),
                },
            );
        }
    }
    decode_satisfying_assignments(
        program,
        facts,
        problem,
        &result.assignments,
        false,
        &undecided,
    )
}

/// Targets in `undecided` take their outcome from it; the assignments do not
/// decide them.
fn decode_satisfying_assignments<E>(
    program: &SelectorProgram,
    facts: &SelectorFactStore,
    problem: &CompiledSelectorProblem,
    assignments: &[BackendAssignment],
    capped: bool,
    undecided: &BTreeMap<SelectorTargetId, ClaimOutcome>,
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
            if undecided.contains_key(&target.id) {
                continue;
            }
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
                owner,
                statement_ordinal,
                binding,
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
                outcome: match undecided.get(&target.id) {
                    Some(outcome) => outcome.clone(),
                    None => claims_to_outcome(
                        claims_by_target.remove(&target.id).unwrap_or_default(),
                        capped,
                    ),
                },
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

/// `capped`: the backend stopped listing some variable's values at
/// `MAX_ALTERNATIVES_PER_VARIABLE`, so a target with that many candidates may have more.
fn claims_to_outcome(claims: Vec<ResolvedClaim>, capped: bool) -> ClaimOutcome {
    match claims.as_slice() {
        [] => ClaimOutcome::NoMatch,
        [claim] => ClaimOutcome::Unique {
            claim: claim.clone(),
        },
        _ => ClaimOutcome::Ambiguous {
            candidates_truncated: capped && claims.len() >= MAX_ALTERNATIVES_PER_VARIABLE as usize,
            candidates: claims,
        },
    }
}

fn unsatisfiable_result(program: &SelectorProgram) -> SolverResult {
    SolverResult {
        claims: program
            .targets
            .iter()
            .map(|target| SolverClaim {
                target: target.id,
                outcome: ClaimOutcome::Unsatisfiable {
                    reason: UNSATISFIABLE_REASON.to_string(),
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

    use analysis::{OwnerId, StatementOrdinal};
    use selector_constraint_backend::ConstraintValue;
    use selector_constraint_backend::{
        BackendAssignment, BackendAssignmentCoverage, BackendSolveResult, BackendSolveStatus,
        BackendValueId, BackendVariableAssignment,
    };
    use selector_ir::{OwnerTerm, SelectorAtom, StringTerm, VariableDomain};

    use super::*;

    #[derive(Debug)]
    struct SelectingBackend {
        assignments: Vec<Vec<(ConstraintVariableId, ConstraintValue)>>,
        coverage: BackendAssignmentCoverage,
        status: BackendSolveStatus,
    }

    impl SelectorProblemBackend for SelectingBackend {
        type Error = Infallible;

        fn solve(
            &self,
            problem: &CompiledSelectorProblem,
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
                fixed_variables: BTreeSet::new(),
            })
        }
    }

    #[derive(Debug)]
    struct PanickingBackend;

    impl SelectorProblemBackend for PanickingBackend {
        type Error = Infallible;

        fn solve(
            &self,
            _problem: &CompiledSelectorProblem,
        ) -> Result<BackendSolveResult, Self::Error> {
            panic!("singleton/no-constraint selector should not invoke backend")
        }
    }

    fn backend_value_for(
        problem: &CompiledSelectorProblem,
        value: &ConstraintValue,
    ) -> BackendValueId {
        problem
            .value_dictionary
            .encode(value)
            .expect("test backend value must be in dictionary")
    }

    fn owner(value: usize) -> ConstraintValue {
        ConstraintValue::Owner(OwnerId(value))
    }

    fn string(value: &str) -> ConstraintValue {
        ConstraintValue::String(value.to_string())
    }

    fn owner_fact(owner: OwnerId, ordinal: usize, kind: &str) -> SelectorFact {
        SelectorFact::Owner {
            owner,
            statement_ordinal: StatementOrdinal(ordinal),
            statement_kind: kind.to_string(),
        }
    }

    fn binding_fact(owner: OwnerId, binding: &str) -> SelectorFact {
        SelectorFact::DeclaredBinding {
            owner,
            binding: binding.to_string(),
        }
    }

    fn binding_program() -> (SelectorProgram, SelectorTargetId) {
        let mut program = SelectorProgram::default();
        let owner = program.add_variable(VariableDomain::Owner, Some("owner".to_string()));
        let binding = program.add_variable(VariableDomain::String, Some("binding".to_string()));
        let target = program.add_target(
            owner,
            "module",
            ClaimKind::Binding {
                export_name: Some("Readable".to_string()),
            },
        );
        program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: OwnerTerm::Var { id: owner },
            binding: StringTerm::Var { id: binding },
        });
        (program, target)
    }

    fn const_binding_program() -> (SelectorProgram, SelectorTargetId) {
        let mut program = SelectorProgram::default();
        let owner = program.add_variable(VariableDomain::Owner, Some("owner".to_string()));
        let target = program.add_target(
            owner,
            "module",
            ClaimKind::Binding {
                export_name: Some("Readable".to_string()),
            },
        );
        program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: OwnerTerm::Var { id: owner },
            binding: StringTerm::Const {
                value: "minA".to_string(),
            },
        });
        (program, target)
    }

    fn duplicate_fixed_target_program() -> (SelectorProgram, SelectorTargetId, SelectorTargetId) {
        let mut program = SelectorProgram::default();
        let first_owner = program.add_variable(VariableDomain::Owner, Some("first".to_string()));
        let second_owner = program.add_variable(VariableDomain::Owner, Some("second".to_string()));
        let first_target = program.add_target(
            first_owner,
            "module",
            ClaimKind::Binding {
                export_name: Some("First".to_string()),
            },
        );
        let second_target = program.add_target(
            second_owner,
            "module",
            ClaimKind::Binding {
                export_name: Some("Second".to_string()),
            },
        );
        for owner in [first_owner, second_owner] {
            program.add_atom(SelectorAtom::OwnerDeclaresBinding {
                owner: OwnerTerm::Var { id: owner },
                binding: StringTerm::Const {
                    value: "minA".to_string(),
                },
            });
        }
        program.require_all_different(vec![first_target, second_target]);
        (program, first_target, second_target)
    }

    fn facts() -> SelectorFactStore {
        let mut facts = SelectorFactStore::default();
        facts.push(owner_fact(OwnerId(1), 10, "function"));
        facts.push(binding_fact(OwnerId(1), "minA"));
        facts.push(owner_fact(OwnerId(2), 20, "function"));
        facts.push(binding_fact(OwnerId(2), "minB"));
        facts
    }

    fn single_binding_facts() -> SelectorFactStore {
        let mut facts = SelectorFactStore::default();
        facts.push(owner_fact(OwnerId(1), 10, "function"));
        facts.push(binding_fact(OwnerId(1), "minA"));
        facts
    }

    /// The outcome of a target resolved to the owner declaring `minA`.
    fn min_a_outcome() -> ClaimOutcome {
        ClaimOutcome::Unique {
            claim: ResolvedClaim {
                owner: OwnerId(1),
                statement_ordinal: StatementOrdinal(10),
                binding: Some("minA".to_string()),
            },
        }
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

        assert_eq!(result.outcome_for(target), Some(&min_a_outcome()));
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

        assert_eq!(result.outcome_for(target), Some(&min_a_outcome()));
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
            Some(ClaimOutcome::Ambiguous {
                candidates,
                candidates_truncated: false,
            }) => {
                assert_eq!(candidates.len(), 2);
                assert!(candidates.iter().any(|claim| claim.owner == OwnerId(1)));
                assert!(candidates.iter().any(|claim| claim.owner == OwnerId(2)));
            }
            other => panic!("expected ambiguous backend result, got {other:?}"),
        }
    }

    fn assert_every_target_unsatisfiable(result: &SolverResult, targets: &[SelectorTargetId]) {
        for target in targets {
            assert!(
                matches!(
                    result.outcome_for(*target),
                    Some(ClaimOutcome::Unsatisfiable { .. })
                ),
                "{target:?}: {result:?}"
            );
        }
    }

    /// Two targets, each over a variable owner and binding, so the program
    /// reaches the backend.
    #[test]
    fn backend_unsatisfiable_status_leaves_every_target_unsatisfiable() {
        let mut program = SelectorProgram::default();
        let targets = ["First", "Second"].map(|export_name| {
            let owner = program.add_variable(VariableDomain::Owner, Some(export_name.to_string()));
            let binding = program.add_variable(VariableDomain::String, None);
            program.add_atom(SelectorAtom::OwnerDeclaresBinding {
                owner: OwnerTerm::Var { id: owner },
                binding: StringTerm::Var { id: binding },
            });
            program.add_target(
                owner,
                "module",
                ClaimKind::Binding {
                    export_name: Some(export_name.to_string()),
                },
            )
        });
        let backend = SelectingBackend {
            status: BackendSolveStatus::Unsatisfiable,
            coverage: BackendAssignmentCoverage::Sample,
            assignments: Vec::new(),
        };

        let result = solve_with_backend(&program, &facts(), &backend).unwrap();

        assert_every_target_unsatisfiable(&result, &targets);
    }

    #[test]
    fn unsatisfiable_status_with_assignments_is_a_backend_error() {
        let (program, _target) = binding_program();
        let backend = SelectingBackend {
            status: BackendSolveStatus::Unsatisfiable,
            coverage: BackendAssignmentCoverage::Sample,
            assignments: vec![vec![(ConstraintVariableId(0), owner(1))]],
        };

        assert!(matches!(
            solve_with_backend(&program, &facts(), &backend),
            Err(SelectorBackendSolveError::UnsatReturnedAssignments)
        ));
    }

    #[test]
    fn sample_coverage_on_a_satisfiable_status_is_a_backend_error() {
        let (program, _target) = binding_program();
        let backend = SelectingBackend {
            status: BackendSolveStatus::Satisfiable,
            coverage: BackendAssignmentCoverage::Sample,
            assignments: vec![vec![
                (ConstraintVariableId(0), owner(1)),
                (ConstraintVariableId(1), string("minA")),
            ]],
        };

        assert!(matches!(
            solve_with_backend(&program, &facts(), &backend),
            Err(SelectorBackendSolveError::SampleCoverage { .. })
        ));
    }

    #[test]
    fn singleton_no_constraint_problem_decodes_without_backend() {
        let (program, target) = const_binding_program();

        let result = solve_with_backend(&program, &facts(), &PanickingBackend).unwrap();

        assert_eq!(result.outcome_for(target), Some(&min_a_outcome()));
    }

    #[test]
    fn singleton_variable_binding_projection_decodes_without_backend() {
        let (program, target) = binding_program();

        let result =
            solve_with_backend(&program, &single_binding_facts(), &PanickingBackend).unwrap();

        assert_eq!(result.outcome_for(target), Some(&min_a_outcome()));
    }

    /// Presolve proves the two fixed targets clash before any backend call.
    #[test]
    fn known_unsat_is_decided_without_the_backend() {
        let (program, first_target, second_target) = duplicate_fixed_target_program();

        let result =
            solve_with_backend(&program, &single_binding_facts(), &PanickingBackend).unwrap();

        assert_every_target_unsatisfiable(&result, &[first_target, second_target]);
    }

    fn fixed_binding_targets(
        program: &mut SelectorProgram,
        bindings: &[&str],
    ) -> Vec<SelectorTargetId> {
        bindings
            .iter()
            .map(|binding| {
                let owner =
                    program.add_variable(VariableDomain::Owner, Some(format!("@{binding}")));
                program.add_atom(SelectorAtom::OwnerDeclaresBinding {
                    owner: OwnerTerm::Var { id: owner },
                    binding: StringTerm::Const {
                        value: binding.to_string(),
                    },
                });
                program.add_target(
                    owner,
                    "module",
                    ClaimKind::Binding {
                        export_name: Some(binding.to_string()),
                    },
                )
            })
            .collect()
    }

    /// Two targets over distinct bindings, decoded from an UNKNOWN response:
    /// only a target whose variables the backend proved fixed resolves,
    /// however many rows agree on the others.
    #[test]
    fn unknown_resolves_only_proven_fixed_targets() {
        let mut program = SelectorProgram::default();
        let targets = fixed_binding_targets(&mut program, &["minA", "minB"]);
        let [fixed, unproven] = targets[..] else {
            unreachable!()
        };
        let mut facts = SelectorFactStore::default();
        for (owner, binding) in [(1, "minA"), (2, "minB")] {
            facts.push(owner_fact(OwnerId(owner), owner * 10, "function"));
            facts.push(binding_fact(OwnerId(owner), binding));
        }
        let problem = compile_selector_problem(&program, &facts).unwrap();
        let owner_variable = |target| {
            problem
                .target_projections
                .iter()
                .find(|projection| projection.target == target)
                .unwrap()
                .owner_variable
        };
        let row = BackendAssignment {
            values: [(fixed, 1), (unproven, 2)]
                .into_iter()
                .map(|(target, value)| BackendVariableAssignment {
                    variable: owner_variable(target),
                    value: backend_value_for(&problem, &owner(value)),
                })
                .collect(),
        };
        let reason = "CP-SAT stopped before proving complete target support";

        let result = decode_backend_result::<Infallible>(
            &program,
            &facts,
            &problem,
            BackendSolveResult {
                status: BackendSolveStatus::Unknown,
                assignment_coverage: BackendAssignmentCoverage::Sample,
                // Both rows agree on `unproven`, but nothing proved it fixed.
                assignments: vec![row.clone(), row],
                diagnostic: Some(reason.to_string()),
                fixed_variables: BTreeSet::from([owner_variable(fixed)]),
            },
        )
        .unwrap();

        assert_eq!(result.outcome_for(fixed), Some(&min_a_outcome()));
        assert_eq!(
            result.outcome_for(unproven),
            Some(&ClaimOutcome::Undecided {
                reason: reason.to_string()
            })
        );
    }

    #[test]
    fn unknown_without_rows_leaves_every_target_undecided() {
        let (program, target) = binding_program();
        let backend = SelectingBackend {
            status: BackendSolveStatus::Unknown,
            coverage: BackendAssignmentCoverage::Sample,
            assignments: Vec::new(),
        };

        let result = solve_with_backend(&program, &facts(), &backend).unwrap();

        assert!(matches!(
            result.outcome_for(target),
            Some(ClaimOutcome::Undecided { .. })
        ));
    }
}
