//! The solver boundary: what a backend returns for a compiled problem.

use std::collections::BTreeSet;

use crate::*;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BackendAssignment {
    pub values: Vec<BackendVariableAssignment>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BackendVariableAssignment {
    pub variable: ConstraintVariableId,
    pub value: BackendValueId,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BackendSolveStatus {
    Unsatisfiable,
    Satisfiable,
    Ambiguous,
    Unknown,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum BackendAssignmentCoverage {
    #[default]
    Sample,
    /// Every value each projected variable can take appears in some assignment.
    TargetSupportComplete,
    /// As `TargetSupportComplete`, except that some ambiguous variable stopped at
    /// `MAX_ALTERNATIVES_PER_VARIABLE` values without proving it has no more.
    TargetSupportCapped,
}

/// Distinct values a backend lists per ambiguous projected variable before it reports
/// `BackendAssignmentCoverage::TargetSupportCapped`: the listing bound of an
/// ambiguous selector outcome.
pub const MAX_ALTERNATIVES_PER_VARIABLE: u32 = selector_outcome::MAX_LISTED_CANDIDATES as u32;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BackendSolveResult {
    pub status: BackendSolveStatus,
    pub assignment_coverage: BackendAssignmentCoverage,
    pub assignments: Vec<BackendAssignment>,
    pub stop_reason: Option<selector_ir::SolverStopReason>,
    /// Only with [`BackendSolveStatus::Unknown`]: the projected variables
    /// proven to take one value before the backend stopped; each takes the
    /// value it has in every assignment. Every other variable is undecided.
    pub fixed_variables: BTreeSet<ConstraintVariableId>,
}

pub trait SelectorProblemBackend {
    type Error;

    fn solve(&self, problem: &CompiledSelectorProblem) -> Result<BackendSolveResult, Self::Error>;
}
