//! Runtime wiring for production selector solving.
//!
//! Selector lowering and fact extraction are owned by callers. This module only
//! reads the CP-SAT settings from the environment and runs the shared backend
//! solver.

use anyhow::{Context, Result};
use selector_ir::{SelectorFactStore, SelectorProgram, SolverResult};
use selector_ortools_cpsat_backend::{CpSatSettings, OrToolsCpSatBackend};

pub fn solve_global_selector_program(
    program: &SelectorProgram,
    facts: &SelectorFactStore,
) -> Result<SolverResult> {
    let backend = OrToolsCpSatBackend::new(CpSatSettings::from_env()?);
    selector_backend_solver::solve_with_backend(program, facts, &backend)
        .with_context(|| "global selector CP-SAT backend failed")
}
