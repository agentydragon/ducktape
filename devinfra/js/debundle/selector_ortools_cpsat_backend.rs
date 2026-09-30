//! OR-Tools CP-SAT implementation of the selector backend contract, linked into
//! this process (<docs/selector_resolution.md> § Inside the resolve).
//!
//! A [`CompiledSelectorProblem`] becomes one `CpModelProto`
//! ([`selector_ortools_cpsat_model`]); [`SupportSearch`] then solves it
//! repeatedly to list the values each projected variable supports.

use std::collections::BTreeSet;
use std::error::Error;
use std::fmt;
use std::num::{NonZeroI32, NonZeroUsize};

use cp_model_proto::operations_research::sat::{ConstraintProto, CpSolverStatus};
use ortools_cpsat_ffi::SolveError;
use sat_parameters_proto::operations_research::sat::SatParameters;
use selector_constraint_backend::{
    BackendAssignment, BackendAssignmentCoverage, BackendSolveResult, BackendSolveStatus,
    BackendValueId, BackendVariableAssignment, CompiledSelectorProblem, ConstraintVariableId,
    MAX_ALTERNATIVES_PER_VARIABLE, SelectorProblemBackend,
};
use selector_ortools_cpsat_model::{InvalidProblem, SelectorCpModel, forbidden_table};

const NUM_SEARCH_WORKERS_ENV: &str = "DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_NUM_SEARCH_WORKERS";
const MAX_TIME_SECONDS_ENV: &str = "DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_MAX_TIME_SECONDS";
const ONE_WORKER: NonZeroI32 = NonZeroI32::new(1).unwrap();

/// CP-SAT settings of every solve of one backend.
#[derive(Debug, Clone, PartialEq)]
pub struct CpSatSettings {
    /// Search threads of one solve. Concurrent solves multiply it.
    num_search_workers: NonZeroI32,
    /// CP-SAT's own wall-clock limit of one solve; none when absent.
    max_time_seconds: Option<f64>,
}

impl Default for CpSatSettings {
    fn default() -> Self {
        Self {
            num_search_workers: ONE_WORKER,
            max_time_seconds: None,
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub struct InvalidSetting {
    name: &'static str,
    value: String,
    expected: &'static str,
}

impl fmt::Display for InvalidSetting {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{} must be {}, got `{}`",
            self.name, self.expected, self.value
        )
    }
}

impl Error for InvalidSetting {}

impl CpSatSettings {
    /// The settings the process environment asks for; an unset or empty
    /// variable keeps its default.
    pub fn from_env() -> Result<Self, InvalidSetting> {
        Self::from_lookup(|name| match std::env::var(name) {
            Ok(value) => Some(value),
            Err(std::env::VarError::NotPresent) => None,
            Err(std::env::VarError::NotUnicode(value)) => {
                Some(value.to_string_lossy().into_owned())
            }
        })
    }

    fn from_lookup(lookup: impl Fn(&str) -> Option<String>) -> Result<Self, InvalidSetting> {
        let lookup = |name| lookup(name).filter(|value| !value.is_empty());
        let mut settings = Self::default();
        if let Some(value) = lookup(NUM_SEARCH_WORKERS_ENV) {
            settings.num_search_workers = value
                .parse::<i32>()
                .ok()
                .filter(|workers| *workers > 0)
                .and_then(NonZeroI32::new)
                .ok_or(InvalidSetting {
                    name: NUM_SEARCH_WORKERS_ENV,
                    value,
                    expected: "a positive integer",
                })?;
        }
        if let Some(value) = lookup(MAX_TIME_SECONDS_ENV) {
            settings.max_time_seconds = Some(
                value
                    .parse::<f64>()
                    .ok()
                    .filter(|seconds| seconds.is_finite() && *seconds > 0.0)
                    .ok_or(InvalidSetting {
                        name: MAX_TIME_SECONDS_ENV,
                        value,
                        expected: "a positive number",
                    })?,
            );
        }
        Ok(settings)
    }

    fn sat_parameters(&self) -> SatParameters {
        SatParameters {
            num_workers: Some(self.num_search_workers.get()),
            max_time_in_seconds: self.max_time_seconds,
            // CP-SAT's default handler overwrites the process's SIGINT
            // disposition and leaves it overwritten after the solve.
            catch_sigint_signal: Some(false),
            ..Default::default()
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct OrToolsCpSatBackend {
    settings: CpSatSettings,
}

impl OrToolsCpSatBackend {
    pub fn new(settings: CpSatSettings) -> Self {
        Self { settings }
    }
}

impl SelectorProblemBackend for OrToolsCpSatBackend {
    type Error = OrToolsCpSatBackendError;

    fn solve(&self, problem: &CompiledSelectorProblem) -> Result<BackendSolveResult, Self::Error> {
        search_support(
            SelectorCpModel::build(problem)?,
            &self.settings.sat_parameters(),
            NonZeroUsize::new(MAX_ALTERNATIVES_PER_VARIABLE as usize)
                .expect("MAX_ALTERNATIVES_PER_VARIABLE is positive"),
        )
    }
}

#[derive(Debug)]
pub enum OrToolsCpSatBackendError {
    InvalidProblem(InvalidProblem),
    /// CP-SAT rejected the model it was given.
    ModelInvalid(String),
    Solve(SolveError),
}

impl fmt::Display for OrToolsCpSatBackendError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidProblem(err) => write!(f, "invalid CP-SAT problem: {err}"),
            Self::ModelInvalid(reason) => write!(f, "CP-SAT rejected the model: {reason}"),
            Self::Solve(err) => write!(f, "CP-SAT solve failed: {err}"),
        }
    }
}

impl Error for OrToolsCpSatBackendError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::InvalidProblem(err) => Some(err),
            Self::Solve(err) => Some(err),
            Self::ModelInvalid(_) => None,
        }
    }
}

impl From<InvalidProblem> for OrToolsCpSatBackendError {
    fn from(err: InvalidProblem) -> Self {
        Self::InvalidProblem(err)
    }
}

impl From<SolveError> for OrToolsCpSatBackendError {
    fn from(err: SolveError) -> Self {
        Self::Solve(err)
    }
}

fn search_support(
    model: SelectorCpModel,
    parameters: &SatParameters,
    max_alternatives: NonZeroUsize,
) -> Result<BackendSolveResult, OrToolsCpSatBackendError> {
    let values = vec![BTreeSet::new(); model.projection.len()];
    let mut search = SupportSearch {
        model,
        parameters,
        max_alternatives,
        rows: BTreeSet::new(),
        values,
        proven_fixed: Vec::new(),
    };
    Ok(match search.run()? {
        Finished::Unsatisfiable => BackendSolveResult {
            status: BackendSolveStatus::Unsatisfiable,
            assignment_coverage: BackendAssignmentCoverage::TargetSupportComplete,
            assignments: Vec::new(),
            diagnostic: None,
            fixed_variables: BTreeSet::new(),
        },
        Finished::Complete { capped } => BackendSolveResult {
            status: if search.rows.len() == 1 {
                BackendSolveStatus::Satisfiable
            } else {
                BackendSolveStatus::Ambiguous
            },
            assignment_coverage: if capped {
                BackendAssignmentCoverage::TargetSupportCapped
            } else {
                BackendAssignmentCoverage::TargetSupportComplete
            },
            assignments: search.assignments(),
            diagnostic: None,
            fixed_variables: BTreeSet::new(),
        },
        Finished::Stopped => BackendSolveResult {
            status: BackendSolveStatus::Unknown,
            assignment_coverage: BackendAssignmentCoverage::Sample,
            diagnostic: Some(
                if search.rows.is_empty() {
                    "CP-SAT returned UNKNOWN"
                } else {
                    "CP-SAT stopped before proving complete target support"
                }
                .to_string(),
            ),
            assignments: search.assignments(),
            fixed_variables: search.proven_fixed.iter().copied().collect(),
        },
    })
}

/// How a support search ended.
enum Finished {
    Unsatisfiable,
    /// Every projected variable is proven fixed or has its alternatives
    /// listed; `capped` when some ambiguous one stopped at the cap instead.
    Complete {
        capped: bool,
    },
    /// CP-SAT stopped before an answer, at a time limit.
    Stopped,
}

enum Step {
    Feasible,
    Infeasible,
    Stopped,
}

/// Collects projected rows until every projected variable is either proven
/// fixed or has its alternative values listed (up to a per-variable cap).
/// Enumerating whole rows is exponential on ambiguous programs; this needs at
/// most (#ambiguous variables + 1) solves to prove which variables are fixed,
/// plus at most `max_alternatives` solves per ambiguous variable.
struct SupportSearch<'a> {
    /// The base model; each solve adds one constraint for its duration.
    model: SelectorCpModel,
    parameters: &'a SatParameters,
    max_alternatives: NonZeroUsize,
    /// Projected rows found so far, deduplicated; a row holds the value of each
    /// `model.projection` variable in order.
    rows: BTreeSet<Vec<i64>>,
    /// Distinct values of `model.projection[i]` across `rows`.
    values: Vec<BTreeSet<i64>>,
    /// The projected variables the settle rounds proved fixed.
    proven_fixed: Vec<ConstraintVariableId>,
}

impl SupportSearch<'_> {
    fn run(&mut self) -> Result<Finished, OrToolsCpSatBackendError> {
        match self.solve(None)? {
            Step::Feasible => {}
            Step::Infeasible => return Ok(Finished::Unsatisfiable),
            Step::Stopped => return Ok(Finished::Stopped),
        }
        let first_row = self
            .rows
            .first()
            .expect("a feasible solve finds a row")
            .clone();

        // A variable is settled while every row found so far agrees with the
        // first row on it. Each feasible round evicts at least one settled
        // variable; an infeasible round proves every remaining settled
        // variable fixed.
        loop {
            let settled = (0..self.values.len())
                .filter(|&i| self.values[i].len() == 1)
                .collect::<Vec<_>>();
            if settled.is_empty() {
                break;
            }
            let forbidden = forbidden_table(
                settled.iter().map(|&i| self.model.projection[i].index),
                settled.iter().map(|&i| first_row[i]).collect(),
            );
            match self.solve(Some(forbidden))? {
                Step::Feasible => {}
                Step::Infeasible => {
                    self.proven_fixed
                        .extend(settled.iter().map(|&i| self.model.projection[i].id));
                    break;
                }
                Step::Stopped => return Ok(Finished::Stopped),
            }
        }

        let mut capped = false;
        for i in 0..self.values.len() {
            if self.values[i].len() == 1 {
                continue;
            }
            let variable = self.model.projection[i];
            // Rows found for other variables may also add values to this one.
            let mut exhausted = self.values[i].len() == variable.domain_size;
            while !exhausted && self.values[i].len() < self.max_alternatives.get() {
                let forbidden =
                    forbidden_table([variable.index], self.values[i].iter().copied().collect());
                match self.solve(Some(forbidden))? {
                    Step::Feasible => exhausted = self.values[i].len() == variable.domain_size,
                    Step::Infeasible => exhausted = true,
                    Step::Stopped => return Ok(Finished::Stopped),
                }
            }
            capped |= !exhausted;
        }
        Ok(Finished::Complete { capped })
    }

    /// Solves the base model plus `constraint`. A feasible solve records its
    /// projected row.
    fn solve(
        &mut self,
        constraint: Option<ConstraintProto>,
    ) -> Result<Step, OrToolsCpSatBackendError> {
        let added = constraint.is_some();
        self.model.proto.constraints.extend(constraint);
        let response = ortools_cpsat_ffi::solve(&self.model.proto, self.parameters);
        if added {
            self.model.proto.constraints.pop();
        }
        let response = response?;
        match response.status() {
            CpSolverStatus::Optimal | CpSolverStatus::Feasible => {
                let row = self
                    .model
                    .projection
                    .iter()
                    .map(|variable| {
                        response.solution[usize::try_from(variable.index)
                            .expect("a variable index is not negative")]
                    })
                    .collect::<Vec<_>>();
                for (values, value) in self.values.iter_mut().zip(&row) {
                    values.insert(*value);
                }
                self.rows.insert(row);
                Ok(Step::Feasible)
            }
            CpSolverStatus::Infeasible => Ok(Step::Infeasible),
            CpSolverStatus::ModelInvalid => Err(OrToolsCpSatBackendError::ModelInvalid(
                response.solution_info,
            )),
            CpSolverStatus::Unknown => Ok(Step::Stopped),
        }
    }

    fn assignments(&self) -> Vec<BackendAssignment> {
        self.rows
            .iter()
            .map(|row| BackendAssignment {
                values: self
                    .model
                    .projection
                    .iter()
                    .zip(row)
                    .map(|(variable, value)| BackendVariableAssignment {
                        variable: variable.id,
                        value: BackendValueId(*value),
                    })
                    .collect(),
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;
    use std::sync::Mutex;
    use std::thread;

    use analysis::{OwnerId, StatementOrdinal};
    use selector_backend_solver::solve_with_backend;
    use selector_constraint_backend::{
        AllDifferentConstraintId, AllowedTupleConstraintId, AllowedTupleRowsId,
        CompiledAllDifferentConstraint, CompiledAllowedTupleConstraint, CompiledAllowedTupleRowSet,
        CompiledAllowedTupleRows, CompiledSharedVariableDomain, CompiledVariable,
        CompiledVariableDomain, DomainValueDictionary, FullDomainValues, SharedVariableDomainId,
        TargetBindingProjection, TargetProjection,
    };
    use selector_ir::{
        ClaimKind, ClaimOutcome, OwnerTerm, ResolvedClaim, SelectorAtom, SelectorFact,
        SelectorFactStore, SelectorProgram, SelectorTargetId, StringTerm, VariableDomain,
    };

    use super::*;

    // Problems built by hand rather than through the model builder, which
    // settles contradictions and singleton domains before any backend sees
    // them: these tests drive the support search itself.

    fn problem() -> CompiledSelectorProblem {
        CompiledSelectorProblem {
            value_dictionary: DomainValueDictionary::default(),
            full_domains: FullDomainValues::default(),
            shared_variable_domains: Vec::new(),
            variables: Vec::new(),
            target_projections: Vec::new(),
            allowed_tuple_row_sets: Vec::new(),
            allowed_tuples: Vec::new(),
            all_different: Vec::new(),
            known_unsat: false,
        }
    }

    fn variable(id: usize, values: CompiledVariableDomain) -> CompiledVariable {
        CompiledVariable {
            id: ConstraintVariableId(id),
            domain: VariableDomain::Owner,
            debug_name: Some(format!("v{id}")),
            values,
        }
    }

    /// Variable `id` over `0..count`.
    fn dense(id: usize, count: i64) -> CompiledVariable {
        sparse(id, &(0..count).collect::<Vec<_>>())
    }

    fn sparse(id: usize, values: &[i64]) -> CompiledVariable {
        variable(
            id,
            CompiledVariableDomain::Sparse(values.iter().copied().map(BackendValueId).collect()),
        )
    }

    fn shared_domain(id: usize, values: &[i64]) -> CompiledSharedVariableDomain {
        CompiledSharedVariableDomain {
            id: SharedVariableDomainId(id),
            domain: VariableDomain::Owner,
            values: values.iter().copied().map(BackendValueId).collect(),
        }
    }

    /// Table constraint `id` over `variables`, allowing the rows of `row_set`.
    fn table(id: usize, variables: &[usize], row_set: usize) -> CompiledAllowedTupleConstraint {
        CompiledAllowedTupleConstraint {
            id: AllowedTupleConstraintId(id),
            variables: variables
                .iter()
                .copied()
                .map(ConstraintVariableId)
                .collect(),
            row_set: AllowedTupleRowsId(row_set),
        }
    }

    fn row_set(id: usize, arity: usize, values: &[i64]) -> CompiledAllowedTupleRowSet {
        CompiledAllowedTupleRowSet {
            id: AllowedTupleRowsId(id),
            rows: CompiledAllowedTupleRows::from_flat_rows(
                arity,
                values.iter().copied().map(BackendValueId).collect(),
            ),
        }
    }

    fn all_different(id: usize, variables: &[usize]) -> CompiledAllDifferentConstraint {
        CompiledAllDifferentConstraint {
            id: AllDifferentConstraintId(id),
            variables: variables
                .iter()
                .copied()
                .map(ConstraintVariableId)
                .collect(),
        }
    }

    fn projections(owners: &[usize]) -> Vec<TargetProjection> {
        owners
            .iter()
            .enumerate()
            .map(|(target, owner)| TargetProjection {
                target: SelectorTargetId(target),
                owner_variable: ConstraintVariableId(*owner),
                binding_projection: None,
            })
            .collect()
    }

    fn solve(problem: &CompiledSelectorProblem) -> BackendSolveResult {
        OrToolsCpSatBackend::default().solve(problem).unwrap()
    }

    fn invalid_problem(problem: &CompiledSelectorProblem) -> String {
        match OrToolsCpSatBackend::default().solve(problem) {
            Err(OrToolsCpSatBackendError::InvalidProblem(err)) => err.to_string(),
            other => panic!("expected an invalid problem, got {other:?}"),
        }
    }

    fn values_of(result: &BackendSolveResult, variable: usize) -> BTreeSet<i64> {
        result
            .assignments
            .iter()
            .flat_map(|row| &row.values)
            .filter(|assignment| assignment.variable == ConstraintVariableId(variable))
            .map(|assignment| assignment.value.0)
            .collect()
    }

    fn row_has(row: &BackendAssignment, variable: usize, value: i64) -> bool {
        row.values.iter().any(|assignment| {
            assignment.variable == ConstraintVariableId(variable) && assignment.value.0 == value
        })
    }

    #[test]
    fn all_different_propagates_broad_specific_fixture() {
        let result = solve(&CompiledSelectorProblem {
            variables: vec![dense(0, 2), dense(1, 2), dense(2, 3)],
            all_different: vec![all_different(0, &[0, 1, 2])],
            allowed_tuple_row_sets: vec![
                row_set(0, 1, &[0, 1]),
                row_set(1, 1, &[1]),
                row_set(2, 1, &[2]),
            ],
            allowed_tuples: vec![table(0, &[0], 0), table(1, &[1], 1), table(2, &[2], 2)],
            target_projections: projections(&[0, 1]),
            ..problem()
        });

        assert_eq!(result.status, BackendSolveStatus::Satisfiable);
        assert_eq!(
            result.assignment_coverage,
            BackendAssignmentCoverage::TargetSupportComplete
        );
        assert_eq!(result.assignments.len(), 1);
        assert!(row_has(&result.assignments[0], 0, 0));
        assert!(row_has(&result.assignments[0], 1, 1));
    }

    #[test]
    fn multiple_projection_rows_are_ambiguous() {
        let result = solve(&CompiledSelectorProblem {
            variables: vec![dense(0, 2)],
            target_projections: projections(&[0]),
            ..problem()
        });

        assert_eq!(result.status, BackendSolveStatus::Ambiguous);
        assert_eq!(
            result.assignment_coverage,
            BackendAssignmentCoverage::TargetSupportComplete
        );
        assert_eq!(result.assignments.len(), 2);
    }

    #[test]
    fn unprojected_variables_do_not_create_rows() {
        let result = solve(&CompiledSelectorProblem {
            variables: vec![dense(0, 1), sparse(1, &[10, 11, 12, 13, 14])],
            target_projections: projections(&[0]),
            ..problem()
        });

        assert_eq!(result.status, BackendSolveStatus::Satisfiable);
        assert_eq!(
            result.assignment_coverage,
            BackendAssignmentCoverage::TargetSupportComplete
        );
        assert_eq!(result.assignments.len(), 1);
        assert!(row_has(&result.assignments[0], 0, 0));
        assert_eq!(result.assignments[0].values.len(), 1);
    }

    #[test]
    fn shared_sparse_domain_can_constrain_multiple_variables() {
        let shared = CompiledVariableDomain::SharedSparse(SharedVariableDomainId(4));
        let result = solve(&CompiledSelectorProblem {
            shared_variable_domains: vec![shared_domain(4, &[1, 3])],
            variables: vec![variable(0, shared.clone()), variable(1, shared)],
            allowed_tuple_row_sets: vec![row_set(0, 1, &[1]), row_set(1, 1, &[3])],
            allowed_tuples: vec![table(0, &[0], 0), table(1, &[1], 1)],
            target_projections: projections(&[0, 1]),
            ..problem()
        });

        assert_eq!(result.status, BackendSolveStatus::Satisfiable);
        assert_eq!(
            result.assignment_coverage,
            BackendAssignmentCoverage::TargetSupportComplete
        );
        assert_eq!(result.assignments.len(), 1);
        assert!(row_has(&result.assignments[0], 0, 1));
        assert!(row_has(&result.assignments[0], 1, 3));
    }

    #[test]
    fn missing_shared_sparse_domain_is_invalid() {
        let message = invalid_problem(&CompiledSelectorProblem {
            variables: vec![variable(
                0,
                CompiledVariableDomain::SharedSparse(SharedVariableDomainId(99)),
            )],
            target_projections: projections(&[0]),
            ..problem()
        });

        assert!(
            message.contains("unknown shared sparse domain 99"),
            "{message}"
        );
    }

    #[test]
    fn conflicting_tables_are_unsat() {
        let result = solve(&CompiledSelectorProblem {
            variables: vec![dense(0, 2)],
            allowed_tuple_row_sets: vec![row_set(0, 1, &[0]), row_set(1, 1, &[1])],
            allowed_tuples: vec![table(0, &[0], 0), table(1, &[0], 1)],
            target_projections: projections(&[0]),
            ..problem()
        });

        assert_eq!(result.status, BackendSolveStatus::Unsatisfiable);
        assert_eq!(
            result.assignment_coverage,
            BackendAssignmentCoverage::TargetSupportComplete
        );
        assert!(result.assignments.is_empty());
    }

    // Targets 0 and 1 can only take value 0 and are kept distinct: no
    // assignment exists, so the response has no rows, not even for the
    // independent target 2.
    #[test]
    fn duplicate_claims_under_all_different_are_unsat() {
        let result = solve(&CompiledSelectorProblem {
            variables: vec![dense(0, 3), dense(1, 3), dense(2, 3)],
            allowed_tuple_row_sets: vec![row_set(0, 1, &[0]), row_set(1, 1, &[0])],
            allowed_tuples: vec![table(0, &[0], 0), table(1, &[1], 1)],
            all_different: vec![all_different(0, &[0, 1])],
            target_projections: projections(&[0, 1, 2]),
            ..problem()
        });

        assert_eq!(result.status, BackendSolveStatus::Unsatisfiable);
        assert_eq!(
            result.assignment_coverage,
            BackendAssignmentCoverage::TargetSupportComplete
        );
        assert!(result.assignments.is_empty());
    }

    #[test]
    fn shared_allowed_row_set_can_constrain_multiple_tables() {
        let result = solve(&CompiledSelectorProblem {
            variables: vec![dense(0, 3), dense(1, 3)],
            allowed_tuple_row_sets: vec![row_set(7, 1, &[1])],
            allowed_tuples: vec![table(0, &[0], 7), table(1, &[1], 7)],
            target_projections: projections(&[0, 1]),
            ..problem()
        });

        assert_eq!(result.status, BackendSolveStatus::Satisfiable);
        assert_eq!(
            result.assignment_coverage,
            BackendAssignmentCoverage::TargetSupportComplete
        );
        assert_eq!(result.assignments.len(), 1);
        assert!(row_has(&result.assignments[0], 0, 1));
        assert!(row_has(&result.assignments[0], 1, 1));
    }

    #[test]
    fn missing_allowed_row_set_is_invalid() {
        let message = invalid_problem(&CompiledSelectorProblem {
            variables: vec![dense(0, 2)],
            allowed_tuples: vec![table(3, &[0], 99)],
            target_projections: projections(&[0]),
            ..problem()
        });

        assert!(message.contains("unknown row set 99"), "{message}");
    }

    #[test]
    fn shared_allowed_row_set_arity_mismatch_is_invalid() {
        let message = invalid_problem(&CompiledSelectorProblem {
            variables: vec![dense(0, 2), dense(1, 2)],
            allowed_tuple_row_sets: vec![row_set(5, 1, &[1])],
            allowed_tuples: vec![table(4, &[0, 1], 5)],
            target_projections: projections(&[0]),
            ..problem()
        });

        assert!(message.contains("row set 5 has arity 1"), "{message}");
        assert!(message.contains("expected 2"), "{message}");
    }

    #[test]
    fn empty_domain_is_invalid() {
        let message = invalid_problem(&CompiledSelectorProblem {
            variables: vec![sparse(0, &[])],
            target_projections: projections(&[0]),
            ..problem()
        });

        assert!(
            message.contains("variable 0 has an empty domain"),
            "{message}"
        );
    }

    #[test]
    fn constant_binding_projection_does_not_add_variable() {
        let result = solve(&CompiledSelectorProblem {
            variables: vec![dense(0, 1)],
            target_projections: vec![TargetProjection {
                target: SelectorTargetId(0),
                owner_variable: ConstraintVariableId(0),
                binding_projection: Some(TargetBindingProjection::Const("minA".to_string())),
            }],
            ..problem()
        });

        assert_eq!(result.status, BackendSolveStatus::Satisfiable);
        assert_eq!(
            result.assignment_coverage,
            BackendAssignmentCoverage::TargetSupportComplete
        );
        assert_eq!(result.assignments.len(), 1);
        assert!(row_has(&result.assignments[0], 0, 0));
        assert_eq!(result.assignments[0].values.len(), 1);
    }

    #[test]
    fn unique_program_returns_one_row() {
        let result = solve(&CompiledSelectorProblem {
            variables: vec![dense(0, 3), dense(1, 3), dense(2, 3)],
            all_different: vec![all_different(0, &[0, 1, 2])],
            allowed_tuple_row_sets: vec![row_set(0, 2, &[2, 0, 2, 2])],
            allowed_tuples: vec![table(0, &[0, 1], 0)],
            target_projections: projections(&[0, 1, 2]),
            ..problem()
        });

        assert_eq!(result.status, BackendSolveStatus::Satisfiable);
        assert_eq!(
            result.assignment_coverage,
            BackendAssignmentCoverage::TargetSupportComplete
        );
        assert_eq!(result.assignments.len(), 1);
        assert!(row_has(&result.assignments[0], 0, 2));
        assert!(row_has(&result.assignments[0], 1, 0));
        assert!(row_has(&result.assignments[0], 2, 1));
    }

    // 5 variables all-different over 5 values have 5! = 120 solutions. Every
    // variable's full value set is listed without enumerating them: one first
    // row, at most one row per variable proven ambiguous, and at most 4 more
    // values per variable.
    #[test]
    fn permutation_lists_every_value_without_enumerating() {
        let result = solve(&CompiledSelectorProblem {
            variables: (0..5).map(|id| dense(id, 5)).collect(),
            all_different: vec![all_different(0, &[0, 1, 2, 3, 4])],
            target_projections: projections(&[0, 1, 2, 3, 4]),
            ..problem()
        });

        assert_eq!(result.status, BackendSolveStatus::Ambiguous);
        assert_eq!(
            result.assignment_coverage,
            BackendAssignmentCoverage::TargetSupportComplete
        );
        assert!(result.assignments.len() <= 1 + 5 + 5 * 4);
        for variable in 0..5 {
            assert_eq!(
                values_of(&result, variable),
                BTreeSet::from([0, 1, 2, 3, 4]),
                "{variable}"
            );
        }
    }

    #[test]
    fn fixed_variable_agrees_across_ambiguous_rows() {
        let result = solve(&CompiledSelectorProblem {
            variables: vec![dense(0, 4), dense(1, 4), dense(2, 4)],
            all_different: vec![all_different(0, &[0, 1, 2])],
            allowed_tuple_row_sets: vec![row_set(0, 1, &[3]), row_set(1, 2, &[0, 1, 1, 0, 1, 2])],
            allowed_tuples: vec![table(0, &[0], 0), table(1, &[1, 2], 1)],
            target_projections: projections(&[0, 1, 2]),
            ..problem()
        });

        assert_eq!(result.status, BackendSolveStatus::Ambiguous);
        assert_eq!(
            result.assignment_coverage,
            BackendAssignmentCoverage::TargetSupportComplete
        );
        assert_eq!(values_of(&result, 0), BTreeSet::from([3]));
        assert_eq!(values_of(&result, 1), BTreeSet::from([0, 1]));
        assert_eq!(values_of(&result, 2), BTreeSet::from([0, 1, 2]));
    }

    #[test]
    fn alternatives_stop_at_the_cap() {
        let result = search_support(
            SelectorCpModel::build(&CompiledSelectorProblem {
                variables: vec![dense(0, 10)],
                target_projections: projections(&[0]),
                ..problem()
            })
            .unwrap(),
            &CpSatSettings::default().sat_parameters(),
            NonZeroUsize::new(3).unwrap(),
        )
        .unwrap();

        assert_eq!(result.status, BackendSolveStatus::Ambiguous);
        assert_eq!(
            result.assignment_coverage,
            BackendAssignmentCoverage::TargetSupportCapped
        );
        assert_eq!(values_of(&result, 0).len(), 3);
    }

    fn env(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let pairs = pairs
            .iter()
            .map(|(name, value)| (name.to_string(), value.to_string()))
            .collect::<Vec<_>>();
        move |name| {
            pairs
                .iter()
                .find(|(key, _)| key == name)
                .map(|(_, value)| value.clone())
        }
    }

    #[test]
    fn settings_default_to_one_worker_and_no_time_limit() {
        let settings = CpSatSettings::from_lookup(env(&[])).unwrap();

        assert_eq!(settings.num_search_workers.get(), 1);
        assert_eq!(settings.max_time_seconds, None);
        // An empty variable is unset, not an invalid value.
        assert_eq!(
            CpSatSettings::from_lookup(env(&[
                (NUM_SEARCH_WORKERS_ENV, ""),
                (MAX_TIME_SECONDS_ENV, "")
            ]))
            .unwrap(),
            settings
        );
    }

    #[test]
    fn num_search_workers_accepts_a_positive_override() {
        let settings = CpSatSettings::from_lookup(env(&[(NUM_SEARCH_WORKERS_ENV, "2")])).unwrap();
        let result = OrToolsCpSatBackend::new(settings)
            .solve(&CompiledSelectorProblem {
                variables: vec![dense(0, 1)],
                target_projections: projections(&[0]),
                ..problem()
            })
            .unwrap();

        assert_eq!(result.status, BackendSolveStatus::Satisfiable);
        assert_eq!(
            result.assignment_coverage,
            BackendAssignmentCoverage::TargetSupportComplete
        );
    }

    #[test]
    fn invalid_settings_name_their_variable() {
        for (name, value) in [
            (NUM_SEARCH_WORKERS_ENV, "0"),
            (NUM_SEARCH_WORKERS_ENV, "-3"),
            (NUM_SEARCH_WORKERS_ENV, "two"),
            (NUM_SEARCH_WORKERS_ENV, "2147483648"),
            (MAX_TIME_SECONDS_ENV, "not-time"),
            (MAX_TIME_SECONDS_ENV, "0"),
            (MAX_TIME_SECONDS_ENV, "NaN"),
            (MAX_TIME_SECONDS_ENV, "inf"),
        ] {
            let err = CpSatSettings::from_lookup(env(&[(name, value)])).unwrap_err();
            assert_eq!(err.to_string().split(' ').next(), Some(name), "{value}");
        }
    }

    #[test]
    fn max_time_seconds_is_cp_sats_own_limit() {
        let settings = CpSatSettings::from_lookup(env(&[(MAX_TIME_SECONDS_ENV, "2.5")])).unwrap();

        assert_eq!(settings.sat_parameters().max_time_in_seconds, Some(2.5));
    }

    // Independent groups solve in parallel in one process (selector_resolve
    // runs one request per group on a rayon pool): concurrent solves must each
    // return what they return alone.
    #[test]
    fn concurrent_solves_each_return_their_own_result() {
        let problems = (2..10)
            .map(|size| CompiledSelectorProblem {
                variables: (0..size).map(|id| dense(id, size as i64)).collect(),
                all_different: vec![all_different(0, &(0..size).collect::<Vec<_>>())],
                target_projections: projections(&(0..size).collect::<Vec<_>>()),
                ..problem()
            })
            .collect::<Vec<_>>();
        let alone = problems.iter().map(solve).collect::<Vec<_>>();
        let together = Mutex::new(vec![None; problems.len()]);

        thread::scope(|scope| {
            for (index, problem) in problems.iter().enumerate() {
                for _ in 0..4 {
                    let together = &together;
                    scope.spawn(move || {
                        let result = solve(problem);
                        let mut together = together.lock().unwrap();
                        assert!(
                            together[index]
                                .as_ref()
                                .is_none_or(|first| *first == result)
                        );
                        together[index] = Some(result);
                    });
                }
            }
        });

        assert_eq!(
            together
                .into_inner()
                .unwrap()
                .into_iter()
                .map(Option::unwrap)
                .collect::<Vec<_>>(),
            alone
        );
    }

    fn owner_fact(owner: usize, ordinal: usize, kind: &str) -> SelectorFact {
        SelectorFact::Owner {
            owner: OwnerId(owner),
            statement_ordinal: StatementOrdinal(ordinal),
            statement_kind: kind.to_string(),
        }
    }

    fn declared_binding(owner: usize, binding: &str) -> SelectorFact {
        SelectorFact::DeclaredBinding {
            owner: OwnerId(owner),
            binding: binding.to_string(),
        }
    }

    fn owner_references_binding(owner: usize, binding: &str) -> SelectorFact {
        SelectorFact::OwnerReferencesBinding {
            owner: OwnerId(owner),
            binding: binding.to_string(),
            edge_kind: "eager_use".to_string(),
        }
    }

    fn member_read(ordinal: usize, object: Option<&str>, member: &str) -> SelectorFact {
        SelectorFact::MemberRead {
            statement_ordinal: StatementOrdinal(ordinal),
            object: object.map(str::to_string),
            member: member.to_string(),
        }
    }

    fn module_member_use(ordinal: usize, module: &str, member: &str) -> SelectorFact {
        SelectorFact::ModuleMemberUse {
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
            argument: argument.to_string(),
            callee_object: callee_object.map(str::to_string),
            callee_member: callee_member.to_string(),
            arg_index,
        }
    }

    #[test]
    fn cpsat_resolves_broad_specific_target_injectivity() {
        let mut program = SelectorProgram::default();
        let broad_owner = program.add_variable(VariableDomain::Owner, Some("broad".to_string()));
        let strict_owner = program.add_variable(VariableDomain::Owner, Some("strict".to_string()));
        let broad_target = program.add_target(
            broad_owner,
            "module",
            ClaimKind::Binding {
                export_name: Some("Broad".to_string()),
            },
        );
        let strict_target = program.add_target(
            strict_owner,
            "module",
            ClaimKind::Binding {
                export_name: Some("Strict".to_string()),
            },
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

        let mut facts = SelectorFactStore::default();
        facts.push(owner_fact(10, 0, "var"));
        facts.push(owner_fact(20, 1, "var"));
        facts.push(declared_binding(10, "shared"));
        facts.push(declared_binding(20, "shared"));
        facts.push(declared_binding(20, "specific"));

        let backend = OrToolsCpSatBackend::default();
        let result = solve_with_backend(&program, &facts, &backend).unwrap();

        assert_eq!(
            result.outcome_for(broad_target),
            Some(&ClaimOutcome::Unique {
                claim: ResolvedClaim {
                    owner: OwnerId(10),
                    statement_ordinal: StatementOrdinal(0),
                    binding: Some("shared".to_string()),
                }
            })
        );
        assert_eq!(
            result.outcome_for(strict_target),
            Some(&ClaimOutcome::Unique {
                claim: ResolvedClaim {
                    owner: OwnerId(20),
                    statement_ordinal: StatementOrdinal(1),
                    binding: Some("specific".to_string()),
                }
            })
        );
    }

    #[test]
    fn cpsat_lists_ambiguous_candidates_next_to_unique_target() {
        let mut program = SelectorProgram::default();
        let binding_target = |program: &mut SelectorProgram, binding: &str| {
            let owner = program.add_variable(VariableDomain::Owner, Some(binding.to_string()));
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
        };
        let pair_target = binding_target(&mut program, "pair");
        let solo_target = binding_target(&mut program, "solo");
        let many_target = binding_target(&mut program, "many");

        let mut facts = SelectorFactStore::default();
        for (owner, binding) in [(10, "pair"), (20, "pair"), (30, "solo")]
            .into_iter()
            .chain((40..47).map(|owner| (owner, "many")))
        {
            facts.push(owner_fact(owner, owner, "var"));
            facts.push(declared_binding(owner, binding));
        }

        let backend = OrToolsCpSatBackend::default();
        let result = solve_with_backend(&program, &facts, &backend).unwrap();

        assert_eq!(
            result.outcome_for(solo_target),
            Some(&ClaimOutcome::Unique {
                claim: ResolvedClaim {
                    owner: OwnerId(30),
                    statement_ordinal: StatementOrdinal(30),
                    binding: Some("solo".to_string()),
                }
            })
        );
        match result.outcome_for(pair_target) {
            Some(ClaimOutcome::Ambiguous {
                candidates,
                candidates_truncated: false,
            }) => {
                let owners = candidates
                    .iter()
                    .map(|candidate| candidate.owner)
                    .collect::<BTreeSet<_>>();
                assert_eq!(owners, BTreeSet::from([OwnerId(10), OwnerId(20)]));
            }
            other => panic!("expected complete ambiguous pair target, got {other:?}"),
        }
        // Seven owners declare `many`; the solver stops listing them at the cap.
        match result.outcome_for(many_target) {
            Some(ClaimOutcome::Ambiguous {
                candidates,
                candidates_truncated: true,
            }) => {
                assert_eq!(candidates.len(), MAX_ALTERNATIVES_PER_VARIABLE as usize);
            }
            other => panic!("expected truncated ambiguous many target, got {other:?}"),
        }
    }

    #[test]
    fn cpsat_solves_cross_ref_anchor_in_one_program() {
        let mut program = SelectorProgram::default();
        let anchor_owner = program.add_variable(VariableDomain::Owner, Some("anchor".to_string()));
        let delegator_owner =
            program.add_variable(VariableDomain::Owner, Some("delegator".to_string()));
        let anchor_target = program.add_target(
            anchor_owner,
            "module",
            ClaimKind::Binding {
                export_name: Some("Anchor".to_string()),
            },
        );
        let delegator_target = program.add_target(
            delegator_owner,
            "module",
            ClaimKind::Binding {
                export_name: Some("Delegator".to_string()),
            },
        );
        program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: OwnerTerm::Var { id: anchor_owner },
            binding: StringTerm::Const {
                value: "a".to_string(),
            },
        });
        program.add_atom(SelectorAtom::OwnerReferencesOwner {
            owner: OwnerTerm::Var {
                id: delegator_owner,
            },
            referenced: OwnerTerm::Var { id: anchor_owner },
        });
        program.add_atom(SelectorAtom::OwnerKind {
            owner: OwnerTerm::Var {
                id: delegator_owner,
            },
            statement_kind: StringTerm::Const {
                value: "fn_decl".to_string(),
            },
        });
        program.require_all_different(vec![anchor_target, delegator_target]);

        let mut facts = SelectorFactStore::default();
        facts.push(owner_fact(1, 1, "var_decl"));
        facts.push(declared_binding(1, "a"));
        facts.push(owner_fact(2, 2, "fn_decl"));
        facts.push(declared_binding(2, "b"));
        facts.push(owner_references_binding(2, "a"));
        facts.push(owner_fact(3, 3, "fn_decl"));
        facts.push(declared_binding(3, "other"));
        facts.push(owner_references_binding(3, "missing"));

        let backend = OrToolsCpSatBackend::default();
        let result = solve_with_backend(&program, &facts, &backend).unwrap();

        assert_eq!(
            result.outcome_for(anchor_target),
            Some(&ClaimOutcome::Unique {
                claim: ResolvedClaim {
                    owner: OwnerId(1),
                    statement_ordinal: StatementOrdinal(1),
                    binding: Some("a".to_string()),
                }
            })
        );
        assert_eq!(
            result.outcome_for(delegator_target),
            Some(&ClaimOutcome::Unique {
                claim: ResolvedClaim {
                    owner: OwnerId(2),
                    statement_ordinal: StatementOrdinal(2),
                    binding: Some("b".to_string()),
                }
            })
        );
    }

    #[test]
    fn cpsat_solves_object_constrained_reads_member_jointly() {
        let mut program = SelectorProgram::default();
        let object_owner = program.add_variable(VariableDomain::Owner, Some("object".to_string()));
        let reader_owner = program.add_variable(VariableDomain::Owner, Some("reader".to_string()));
        let object_target = program.add_target(
            object_owner,
            "module",
            ClaimKind::Binding {
                export_name: Some("Context".to_string()),
            },
        );
        let reader_target = program.add_target(
            reader_owner,
            "module",
            ClaimKind::Binding {
                export_name: Some("ReadId".to_string()),
            },
        );
        program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: OwnerTerm::Var { id: object_owner },
            binding: StringTerm::Const {
                value: "ctx".to_string(),
            },
        });
        program.add_atom(SelectorAtom::ReadsMemberOfOwner {
            owner: OwnerTerm::Var { id: reader_owner },
            object: OwnerTerm::Var { id: object_owner },
            member: StringTerm::Const {
                value: "id".to_string(),
            },
        });
        program.add_atom(SelectorAtom::OwnerKind {
            owner: OwnerTerm::Var { id: reader_owner },
            statement_kind: StringTerm::Const {
                value: "fn_decl".to_string(),
            },
        });
        program.require_all_different(vec![object_target, reader_target]);

        let mut facts = SelectorFactStore::default();
        facts.push(owner_fact(1, 1, "var_decl"));
        facts.push(declared_binding(1, "ctx"));
        facts.push(owner_fact(2, 2, "fn_decl"));
        facts.push(declared_binding(2, "readId"));
        facts.push(member_read(2, Some("ctx"), "id"));
        facts.push(owner_fact(3, 3, "fn_decl"));
        facts.push(declared_binding(3, "other"));
        facts.push(member_read(3, Some("otherCtx"), "id"));

        let backend = OrToolsCpSatBackend::default();
        let result = solve_with_backend(&program, &facts, &backend).unwrap();

        assert_eq!(
            result.outcome_for(object_target),
            Some(&ClaimOutcome::Unique {
                claim: ResolvedClaim {
                    owner: OwnerId(1),
                    statement_ordinal: StatementOrdinal(1),
                    binding: Some("ctx".to_string()),
                }
            })
        );
        assert_eq!(
            result.outcome_for(reader_target),
            Some(&ClaimOutcome::Unique {
                claim: ResolvedClaim {
                    owner: OwnerId(2),
                    statement_ordinal: StatementOrdinal(2),
                    binding: Some("readId".to_string()),
                }
            })
        );
    }

    #[test]
    fn cpsat_solves_consumes_module_member_allowed_tuple() {
        let mut program = SelectorProgram::default();
        let consumer_owner =
            program.add_variable(VariableDomain::Owner, Some("consumer".to_string()));
        let consumer_target = program.add_target(
            consumer_owner,
            "module",
            ClaimKind::Binding {
                export_name: Some("WidgetConsumer".to_string()),
            },
        );
        program.add_atom(SelectorAtom::ConsumesModuleMember {
            owner: OwnerTerm::Var { id: consumer_owner },
            module: StringTerm::Const {
                value: "./accessors".to_string(),
            },
            member: StringTerm::Const {
                value: "Widget".to_string(),
            },
        });

        let mut facts = SelectorFactStore::default();
        facts.push(owner_fact(65, 55, "function"));
        facts.push(declared_binding(65, "useWidget"));
        facts.push(module_member_use(55, "./accessors", "Widget"));
        facts.push(owner_fact(66, 56, "function"));
        facts.push(declared_binding(66, "useOther"));
        facts.push(module_member_use(56, "./accessors", "Other"));
        facts.push(owner_fact(67, 57, "function"));
        facts.push(declared_binding(67, "useOtherModule"));
        facts.push(module_member_use(57, "./other", "Widget"));

        let backend = OrToolsCpSatBackend::default();
        let result = solve_with_backend(&program, &facts, &backend).unwrap();

        assert_eq!(
            result.outcome_for(consumer_target),
            Some(&ClaimOutcome::Unique {
                claim: ResolvedClaim {
                    owner: OwnerId(65),
                    statement_ordinal: StatementOrdinal(55),
                    binding: Some("useWidget".to_string()),
                }
            })
        );
    }

    #[test]
    fn cpsat_solves_passed_to_call_of_owner_allowed_tuple() {
        let mut program = SelectorProgram::default();
        let registry_owner =
            program.add_variable(VariableDomain::Owner, Some("registry".to_string()));
        let widget_owner = program.add_variable(VariableDomain::Owner, Some("widget".to_string()));
        let registry_target = program.add_target(
            registry_owner,
            "module",
            ClaimKind::Binding {
                export_name: Some("Registry".to_string()),
            },
        );
        let widget_target = program.add_target(
            widget_owner,
            "module",
            ClaimKind::Binding {
                export_name: Some("RegisteredWidget".to_string()),
            },
        );
        program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: OwnerTerm::Var { id: registry_owner },
            binding: StringTerm::Const {
                value: "registry".to_string(),
            },
        });
        program.add_atom(SelectorAtom::PassedToCallOfOwner {
            owner: OwnerTerm::Var { id: widget_owner },
            callee_object: OwnerTerm::Var { id: registry_owner },
            callee_member: StringTerm::Const {
                value: "register".to_string(),
            },
            arg_index: Some(1),
        });
        program.require_all_different(vec![registry_target, widget_target]);

        let mut facts = SelectorFactStore::default();
        facts.push(owner_fact(10, 0, "var_decl"));
        facts.push(declared_binding(10, "registry"));
        facts.push(owner_fact(20, 1, "class"));
        facts.push(declared_binding(20, "Widget"));
        facts.push(call_argument_use("Widget", Some("registry"), "register", 1));
        facts.push(owner_fact(30, 2, "class"));
        facts.push(declared_binding(30, "WrongIndex"));
        facts.push(call_argument_use(
            "WrongIndex",
            Some("registry"),
            "register",
            0,
        ));
        facts.push(owner_fact(40, 3, "class"));
        facts.push(declared_binding(40, "WrongObject"));
        facts.push(call_argument_use(
            "WrongObject",
            Some("otherRegistry"),
            "register",
            1,
        ));

        let backend = OrToolsCpSatBackend::default();
        let result = solve_with_backend(&program, &facts, &backend).unwrap();

        assert_eq!(
            result.outcome_for(registry_target),
            Some(&ClaimOutcome::Unique {
                claim: ResolvedClaim {
                    owner: OwnerId(10),
                    statement_ordinal: StatementOrdinal(0),
                    binding: Some("registry".to_string()),
                }
            })
        );
        assert_eq!(
            result.outcome_for(widget_target),
            Some(&ClaimOutcome::Unique {
                claim: ResolvedClaim {
                    owner: OwnerId(20),
                    statement_ordinal: StatementOrdinal(1),
                    binding: Some("Widget".to_string()),
                }
            })
        );
    }
}
