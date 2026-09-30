//! Runtime wiring for production selector solving.
//!
//! Selector lowering and fact extraction are owned by callers. This module only
//! chooses the backend executable and runs the shared backend solver.

use std::io;
use std::path::PathBuf;

use anyhow::{Context, Result, bail};
use selector_ir::{SelectorFactStore, SelectorProgram, SolverResult};
use selector_ortools_cpsat_backend::OrToolsCpSatBackend;

const ORTOOLS_CPSAT_SOLVER_ENV: &str = "DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_SOLVER";
const ORTOOLS_CPSAT_SOLVER_FILE: &str = "selector_cpsat_solver";
const ORTOOLS_CPSAT_SOLVER_RUNFILES_DIR: &str =
    "_main/devinfra/js/debundle/solver_backends/ortools_cpsat";

/// The sidecar is, in order: the file `ORTOOLS_CPSAT_SOLVER_ENV` names, the
/// Bazel runfile, the file of the same name beside the running executable (a
/// release download; docs/cli.md § Selector sidecar).
fn ortools_cpsat_solver_from_env() -> Result<PathBuf> {
    if let Ok(solver_path) = std::env::var(ORTOOLS_CPSAT_SOLVER_ENV) {
        return parse_ortools_cpsat_solver_path(Some(&solver_path));
    }
    find_ortools_cpsat_solver(
        std::env::var_os("RUNFILES_DIR").map(PathBuf::from),
        std::env::current_exe(),
    )
}

fn parse_ortools_cpsat_solver_path(solver_path: Option<&str>) -> Result<PathBuf> {
    let solver_path = solver_path
        .map(str::trim)
        .filter(|path| !path.is_empty())
        .with_context(|| {
            format!("{ORTOOLS_CPSAT_SOLVER_ENV} must point at {ORTOOLS_CPSAT_SOLVER_FILE}")
        })?;
    Ok(PathBuf::from(solver_path))
}

fn find_ortools_cpsat_solver(
    runfiles_dir: Option<PathBuf>,
    current_exe: io::Result<PathBuf>,
) -> Result<PathBuf> {
    let mut searched = vec![format!("{ORTOOLS_CPSAT_SOLVER_ENV} (unset)")];
    match runfiles_dir {
        Some(runfiles_dir) => {
            let path = runfiles_dir
                .join(ORTOOLS_CPSAT_SOLVER_RUNFILES_DIR)
                .join(ORTOOLS_CPSAT_SOLVER_FILE);
            if path.is_file() {
                return Ok(path);
            }
            searched.push(path.display().to_string());
        }
        None => searched.push("RUNFILES_DIR (unset)".to_string()),
    }
    match current_exe {
        Ok(exe) => {
            let path = exe.with_file_name(ORTOOLS_CPSAT_SOLVER_FILE);
            if path.is_file() {
                return Ok(path);
            }
            searched.push(path.display().to_string());
        }
        Err(error) => searched.push(format!(
            "the directory of the running executable (unknown: {error})"
        )),
    }
    bail!(
        "CP-SAT sidecar {ORTOOLS_CPSAT_SOLVER_FILE} not found; looked at: {}. Place it \
         beside the `debundle` binary or set {ORTOOLS_CPSAT_SOLVER_ENV} to its path; it is an \
         asset of the same `debundle-*` release as the binary: \
         https://github.com/agentydragon/ducktape/releases",
        searched.join(", ")
    )
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
    use std::fs;
    use std::path::Path;

    use super::*;

    fn touch(path: &Path) {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, "").unwrap();
    }

    #[test]
    fn ortools_cpsat_solver_prefers_the_runfile_over_the_executable_sibling() {
        let runfiles = tempfile::tempdir().unwrap();
        let bin_dir = tempfile::tempdir().unwrap();
        let runfile = runfiles
            .path()
            .join(ORTOOLS_CPSAT_SOLVER_RUNFILES_DIR)
            .join(ORTOOLS_CPSAT_SOLVER_FILE);
        touch(&runfile);
        touch(&bin_dir.path().join(ORTOOLS_CPSAT_SOLVER_FILE));
        assert_eq!(
            find_ortools_cpsat_solver(
                Some(runfiles.path().to_path_buf()),
                Ok(bin_dir.path().join("debundle"))
            )
            .unwrap(),
            runfile
        );
    }

    #[test]
    fn ortools_cpsat_solver_falls_back_to_the_executable_sibling_past_empty_runfiles() {
        let runfiles = tempfile::tempdir().unwrap();
        let bin_dir = tempfile::tempdir().unwrap();
        let sibling = bin_dir.path().join(ORTOOLS_CPSAT_SOLVER_FILE);
        touch(&sibling);
        assert_eq!(
            find_ortools_cpsat_solver(
                Some(runfiles.path().to_path_buf()),
                Ok(bin_dir.path().join("debundle"))
            )
            .unwrap(),
            sibling
        );
    }

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
