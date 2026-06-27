//! Runtime wiring for production selector solving.
//!
//! Selector lowering and fact extraction are owned by callers. This module only
//! chooses the backend executable and runs the shared backend solver.

use std::path::PathBuf;

use anyhow::{Context, Result, bail};
use selector_ir::{SelectorFactStore, SelectorProgram, SolverResult};
use selector_ortools_cpsat_backend::OrToolsCpSatBackend;

const ORTOOLS_CPSAT_SOLVER_ENV: &str = "DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_SOLVER";
const ORTOOLS_CPSAT_SOLVER_RUNFILE: &str =
    "_main/devinfra/js/debundle/solver_backends/ortools_cpsat/selector_cpsat_solver";

fn ortools_cpsat_solver_from_env() -> Result<PathBuf> {
    if let Ok(solver_path) = std::env::var(ORTOOLS_CPSAT_SOLVER_ENV) {
        return parse_ortools_cpsat_solver_path(Some(&solver_path));
    }
    if let Some(solver_path) = ortools_cpsat_solver_from_runfiles() {
        return Ok(solver_path);
    }
    bail!("{ORTOOLS_CPSAT_SOLVER_ENV} must point at selector_cpsat_solver")
}

fn parse_ortools_cpsat_solver_path(solver_path: Option<&str>) -> Result<PathBuf> {
    let solver_path = solver_path
        .map(str::trim)
        .filter(|path| !path.is_empty())
        .with_context(|| {
            format!("{ORTOOLS_CPSAT_SOLVER_ENV} must point at selector_cpsat_solver")
        })?;
    Ok(PathBuf::from(solver_path))
}

fn ortools_cpsat_solver_from_runfiles() -> Option<PathBuf> {
    let runfiles_dir = std::env::var_os("RUNFILES_DIR").map(PathBuf::from)?;
    let path = runfiles_dir.join(ORTOOLS_CPSAT_SOLVER_RUNFILE);
    path.exists().then_some(path)
}

pub fn solve_global_selector_program(
    program: &SelectorProgram,
    facts: &SelectorFactStore,
) -> Result<SolverResult> {
    let backend = OrToolsCpSatBackend::new(ortools_cpsat_solver_from_env()?);
    selector_backend_solver::solve_with_backend(program, facts, &backend)
        .with_context(|| "global selector CP-SAT backend failed")
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use super::*;

    #[test]
    fn ortools_cpsat_solver_path_accepts_non_empty_path() {
        assert_eq!(
            parse_ortools_cpsat_solver_path(Some(" /tmp/solver ")).unwrap(),
            PathBuf::from("/tmp/solver")
        );
    }

    #[test]
    fn ortools_cpsat_solver_path_requires_sidecar_path() {
        let error =
            parse_ortools_cpsat_solver_path(None).expect_err("missing sidecar path should fail");
        assert!(
            error
                .to_string()
                .contains("DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_SOLVER"),
            "{error}"
        );
        let error =
            parse_ortools_cpsat_solver_path(Some(" ")).expect_err("empty sidecar path should fail");
        assert!(
            error
                .to_string()
                .contains("DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_SOLVER"),
            "{error}"
        );
    }
}
