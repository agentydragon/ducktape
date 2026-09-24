//! Backend-backed selector solver entry point.
//!
//! This module is the narrow bridge from selector IR/facts to a finite-domain
//! backend. It does not choose assignments itself: it lowers to a compact
//! compiled problem, calls a backend, and decodes the backend's assignment into
//! the existing materializer-facing `SolverResult`.

use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};

use analysis::{OwnerId, StatementOrdinal};
use selector_constraint_backend::{
    BackendAssignment, BackendAssignmentCoverage, BackendAssignmentError, BackendSolveResult,
    BackendSolveStatus, BackendVariableAssignment, CompiledSelectorProblem, ConstraintValue,
    ConstraintVariableId, PresolveScope, SelectorProblemBackend, TargetBindingProjection,
};
use selector_constraint_model_builder::{
    CompiledSelectorProblemBuildError, SelectorModelBuildSummary, compile_selector_problem,
    compile_selector_problem_with_summary,
};
use selector_ir::{
    ClaimKind, ClaimOutcome, ResolvedClaim, SelectorAtom, SelectorFact, SelectorFactStore,
    SelectorProgram, SelectorSourceMatchProjectionEvent, SelectorSourceMatchProjectionOutcome,
    SelectorTargetId, SolverClaim, SolverGlobalDiagnostic, SolverResult,
};

const SUMMARY_JSON_ENV: &str = "DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_SUMMARY_JSON";
const SUMMARY_JSON_DIR_ENV: &str = "DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_SUMMARY_JSON_DIR";
static SUMMARY_SEQUENCE: AtomicUsize = AtomicUsize::new(0);

pub fn compile_backend_problem(
    program: &SelectorProgram,
    facts: &SelectorFactStore,
) -> Result<CompiledSelectorProblem, CompiledSelectorProblemBuildError> {
    compile_selector_problem(program, facts, PresolveScope::AcrossTargets)
}

pub fn solve_with_backend<B>(
    program: &SelectorProgram,
    facts: &SelectorFactStore,
    backend: &B,
) -> Result<SolverResult, SelectorBackendSolveError<B::Error>>
where
    B: SelectorProblemBackend,
{
    write_selector_build_summary(program, facts, None, None)
        .map_err(SelectorBackendSolveError::Summary)?;
    let problem = compile_and_summarize(program, facts, PresolveScope::AcrossTargets)?;
    if problem.known_unsat.is_some() {
        return solve_localizing_conflicts(program, facts, backend);
    }
    if let Some(result) = singleton_no_constraint_backend_result(&problem) {
        return decode_backend_result(program, facts, &problem, result);
    }
    let result = backend
        .solve(&problem)
        .map_err(SelectorBackendSolveError::Backend)?;
    if result.status == BackendSolveStatus::Unsatisfiable {
        if !result.assignments.is_empty() {
            return Err(SelectorBackendSolveError::UnsatReturnedAssignments);
        }
        return solve_localizing_conflicts(program, facts, backend);
    }
    decode_backend_result(program, facts, &problem, result)
}

fn compile_and_summarize<E>(
    program: &SelectorProgram,
    facts: &SelectorFactStore,
    presolve_scope: PresolveScope,
) -> Result<CompiledSelectorProblem, SelectorBackendSolveError<E>> {
    let compiled = compile_selector_problem_with_summary(program, facts, presolve_scope)
        .map_err(SelectorBackendSolveError::Build)?;
    write_selector_build_summary(
        program,
        facts,
        Some(&compiled.summary),
        Some(&compiled.problem),
    )
    .map_err(SelectorBackendSolveError::Summary)?;
    Ok(compiled.problem)
}

/// The program is unsatisfiable as presolved across targets. Recompile it
/// presolved within targets, so every constraint stays attributable, and let
/// the backend report the conflicting targets while it solves the rest.
fn solve_localizing_conflicts<B>(
    program: &SelectorProgram,
    facts: &SelectorFactStore,
    backend: &B,
) -> Result<SolverResult, SelectorBackendSolveError<B::Error>>
where
    B: SelectorProblemBackend,
{
    let problem = compile_and_summarize(program, facts, PresolveScope::WithinTargets)?;
    if let Some(reason) = &problem.known_unsat {
        // Within targets, presolve only empties what no target owns.
        return Ok(no_match_result(program, Some(reason.clone())));
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
    if problem.known_unsat.is_some()
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
        solver_response_stats: None,
        conflicts: Vec::new(),
    })
}

#[derive(Debug)]
pub enum SelectorBackendSolveError<E> {
    Build(CompiledSelectorProblemBuildError),
    Backend(E),
    Summary(SelectorBuildSummaryError),
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
    UnknownConflictTarget {
        target: SelectorTargetId,
    },
}

impl<E: fmt::Display> fmt::Display for SelectorBackendSolveError<E> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Build(err) => write!(f, "{err}"),
            Self::Backend(err) => write!(f, "selector backend failed: {err}"),
            Self::Summary(err) => write!(f, "{err}"),
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
            Self::UnknownConflictTarget { target } => {
                write!(
                    f,
                    "selector backend reported a conflict for unknown target {target:?}"
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
            Self::Summary(err) => Some(err),
            Self::Assignment(err) => Some(err),
            Self::MissingTargetProjection { .. }
            | Self::MissingAssignmentVariable { .. }
            | Self::DecodedAssignmentDomainMismatch { .. }
            | Self::MissingOwnerFact { .. }
            | Self::EmptySatisfyingAssignments { .. }
            | Self::UnsatReturnedAssignments
            | Self::UnknownConflictTarget { .. } => None,
        }
    }
}

#[derive(Debug)]
pub enum SelectorBuildSummaryError {
    Io {
        path: PathBuf,
        source: io::Error,
    },
    Json {
        path: PathBuf,
        source: serde_json::Error,
    },
}

impl fmt::Display for SelectorBuildSummaryError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io { path, source } => {
                write!(
                    f,
                    "failed to write selector build summary at {}: {source}",
                    path.display()
                )
            }
            Self::Json { path, source } => {
                write!(
                    f,
                    "failed to serialize selector build summary at {}: {source}",
                    path.display()
                )
            }
        }
    }
}

impl Error for SelectorBuildSummaryError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Io { source, .. } => Some(source),
            Self::Json { source, .. } => Some(source),
        }
    }
}

fn write_selector_build_summary(
    program: &SelectorProgram,
    facts: &SelectorFactStore,
    model_summary: Option<&SelectorModelBuildSummary>,
    problem: Option<&CompiledSelectorProblem>,
) -> Result<(), SelectorBuildSummaryError> {
    let paths = selector_build_summary_paths();
    if paths.is_empty() {
        return Ok(());
    }

    let summary = selector_build_summary_json(program, facts, model_summary, problem);
    for path in paths {
        write_summary_json(&path, &summary)?;
    }
    Ok(())
}

fn selector_build_summary_paths() -> Vec<PathBuf> {
    let mut paths = Vec::new();
    if let Some(path) = env_path(SUMMARY_JSON_ENV) {
        paths.push(path);
    }
    if let Some(dir) = env_path(SUMMARY_JSON_DIR_ENV) {
        let sequence = SUMMARY_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        paths.push(dir.join(format!("selector-pre-solver-{sequence:06}.json")));
    }
    paths
}

fn env_path(env: &str) -> Option<PathBuf> {
    std::env::var_os(env)
        .map(PathBuf::from)
        .filter(|path| !path.as_os_str().is_empty())
}

fn write_summary_json(
    path: &Path,
    summary: &serde_json::Value,
) -> Result<(), SelectorBuildSummaryError> {
    if let Some(parent) = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
    {
        fs::create_dir_all(parent).map_err(|source| SelectorBuildSummaryError::Io {
            path: parent.to_path_buf(),
            source,
        })?;
    }
    let temp_path = path.with_extension("json.tmp");
    let mut file =
        fs::File::create(&temp_path).map_err(|source| SelectorBuildSummaryError::Io {
            path: temp_path.clone(),
            source,
        })?;
    serde_json::to_writer_pretty(&mut file, summary).map_err(|source| {
        SelectorBuildSummaryError::Json {
            path: temp_path.clone(),
            source,
        }
    })?;
    file.write_all(b"\n")
        .map_err(|source| SelectorBuildSummaryError::Io {
            path: temp_path.clone(),
            source,
        })?;
    file.flush()
        .map_err(|source| SelectorBuildSummaryError::Io {
            path: temp_path.clone(),
            source,
        })?;
    drop(file);
    fs::rename(&temp_path, path).map_err(|source| SelectorBuildSummaryError::Io {
        path: path.to_path_buf(),
        source,
    })
}

fn selector_build_summary_json(
    program: &SelectorProgram,
    facts: &SelectorFactStore,
    model_summary: Option<&SelectorModelBuildSummary>,
    problem: Option<&CompiledSelectorProblem>,
) -> serde_json::Value {
    let compiled_problem = problem.map(compiled_problem_summary_json);
    serde_json::json!({
        "summary_kind": "selector_pre_solver",
        "stage": if problem.is_some() { "compiled_problem" } else { "input" },
        "selector_program": selector_program_summary_json(program),
        "facts": {
            "total": facts.len(),
            "count_by_relation": facts.counts_by_relation(),
        },
        "model_build": model_summary.map(model_build_summary_json),
        "compiled_problem": compiled_problem,
    })
}

fn selector_program_summary_json(program: &SelectorProgram) -> serde_json::Value {
    serde_json::json!({
        "variable_count": program.variables.len(),
        "variable_count_by_domain": keyed_count(program.variables.iter().map(|variable| variable_domain_name(variable.domain))),
        "target_count": program.targets.len(),
        "target_count_by_claim_kind": keyed_count(program.targets.iter().map(|target| claim_kind_name(&target.claim))),
        "atom_count": program.atoms.len(),
        "atom_count_by_kind": keyed_count(program.atoms.iter().map(selector_atom_kind_name)),
        "source_match_projection": source_match_projection_summary_json(&program.source_match_projection),
        "all_different_count": program.all_different.len(),
        "all_different_variables_count": program.all_different_variables.len(),
        "all_different_arity_histogram": usize_histogram(program.all_different.iter().map(Vec::len)),
        "all_different_variable_arity_histogram": usize_histogram(
            program
                .all_different_variables
                .iter()
                .map(|group| group.variables.len())
        ),
    })
}

fn source_match_projection_summary_json(
    events: &[SelectorSourceMatchProjectionEvent],
) -> serde_json::Value {
    let mut count_by_outcome = BTreeMap::<&'static str, usize>::new();
    let mut count_by_selector_kind = BTreeMap::<String, usize>::new();
    let mut count_by_reason_category = BTreeMap::<String, usize>::new();
    let mut candidate_count_by_outcome = BTreeMap::<&'static str, usize>::new();
    let mut projected_row_count_by_outcome = BTreeMap::<&'static str, usize>::new();
    for event in events {
        let outcome = source_match_projection_outcome_name(&event.outcome);
        *count_by_outcome.entry(outcome).or_insert(0) += 1;
        *count_by_selector_kind
            .entry(event.selector_kind.clone())
            .or_insert(0) += 1;
        *count_by_reason_category
            .entry(event.reason_category.clone())
            .or_insert(0) += 1;
        *candidate_count_by_outcome.entry(outcome).or_insert(0) +=
            event.candidate_count.unwrap_or(0);
        *projected_row_count_by_outcome.entry(outcome).or_insert(0) +=
            event.projected_row_count.unwrap_or(0);
    }
    serde_json::json!({
        "event_count": events.len(),
        "count_by_outcome": count_by_outcome,
        "count_by_selector_kind": count_by_selector_kind,
        "count_by_reason_category": count_by_reason_category,
        "candidate_count_by_outcome": candidate_count_by_outcome,
        "projected_row_count_by_outcome": projected_row_count_by_outcome,
        "events": events,
    })
}

fn source_match_projection_outcome_name(
    outcome: &SelectorSourceMatchProjectionOutcome,
) -> &'static str {
    match outcome {
        SelectorSourceMatchProjectionOutcome::Projected => "projected",
        SelectorSourceMatchProjectionOutcome::NotProjected => "not_projected",
    }
}

fn model_build_summary_json(summary: &SelectorModelBuildSummary) -> serde_json::Value {
    serde_json::json!({
        "domain_value_counts": summary.domain_value_counts,
        "stored_relation_counts": summary.stored_relation_counts,
        "derived_relation_counts": summary.derived_relation_counts,
        "timings_ms": summary.timings_ms,
    })
}

fn compiled_problem_summary_json(problem: &CompiledSelectorProblem) -> serde_json::Value {
    let constraint_count_by_kind = BTreeMap::from([
        ("allowed_table", problem.allowed_tuples.len()),
        ("all_different", problem.all_different.len()),
    ]);
    let domain_sizes = problem
        .variables
        .iter()
        .map(|variable| problem.variable_domain_value_count(variable))
        .collect::<Vec<_>>();
    let allowed_table_rows = problem
        .allowed_tuples
        .iter()
        .map(|constraint| problem.allowed_tuple_rows(constraint).len())
        .collect::<Vec<_>>();
    let allowed_table_cells = problem
        .allowed_tuples
        .iter()
        .map(|constraint| problem.allowed_tuple_rows(constraint).cell_count())
        .collect::<Vec<_>>();
    let shared_allowed_row_count = problem
        .allowed_tuple_row_sets
        .iter()
        .map(|row_set| row_set.rows.len())
        .sum::<usize>();
    let shared_allowed_cell_count = problem
        .allowed_tuple_row_sets
        .iter()
        .map(|row_set| row_set.rows.cell_count())
        .sum::<usize>();
    let shared_sparse_domain_value_count = problem
        .shared_variable_domains
        .iter()
        .map(|domain| domain.values.len())
        .sum::<usize>();

    serde_json::json!({
        "known_unsat": problem.known_unsat.is_some(),
        "known_unsat_reason": problem.known_unsat.as_deref(),
        "variable_count": problem.variables.len(),
        "variable_count_by_domain": keyed_count(problem.variables.iter().map(|variable| variable_domain_name(variable.domain))),
        "variable_domain_representation_count_by_kind": keyed_count(problem.variables.iter().map(|variable| match &variable.values {
            selector_constraint_backend::CompiledVariableDomain::Full(_) => "full",
            selector_constraint_backend::CompiledVariableDomain::Sparse(_) => "sparse",
            selector_constraint_backend::CompiledVariableDomain::SharedSparse(_) => "shared_sparse",
        })),
        "domain_size_histogram": usize_histogram(domain_sizes.iter().copied()),
        "max_domain_values": domain_sizes.into_iter().max().unwrap_or(0),
        "full_domain_value_counts": {
            "owner": problem.full_domains.owners.len(),
            "string": problem.full_domains.strings.len(),
        },
        "value_dictionary_count": problem.value_dictionary.total_len(),
        "shared_sparse_domain_count": problem.shared_variable_domains.len(),
        "shared_sparse_domain_value_count": shared_sparse_domain_value_count,
        "constraint_count_by_kind": constraint_count_by_kind,
        "allowed_table_count": problem.allowed_tuples.len(),
        "allowed_row_set_count": problem.allowed_tuple_row_sets.len(),
        "allowed_table_arity_histogram": usize_histogram(problem.allowed_tuples.iter().map(|constraint| constraint.variables.len())),
        "allowed_table_row_count_histogram": usize_histogram(allowed_table_rows.iter().copied()),
        "allowed_table_cell_count_histogram": usize_histogram(allowed_table_cells.iter().copied()),
        "allowed_row_count": allowed_table_rows.into_iter().sum::<usize>(),
        "allowed_cell_count": allowed_table_cells.into_iter().sum::<usize>(),
        "shared_allowed_row_count": shared_allowed_row_count,
        "shared_allowed_cell_count": shared_allowed_cell_count,
        "all_different_count": problem.all_different.len(),
        "all_different_count_by_reason": keyed_count(problem.all_different.iter().map(|constraint| all_different_reason_name(&constraint.reason))),
        "all_different_arity_histogram": usize_histogram(problem.all_different.iter().map(|constraint| constraint.variables.len())),
        "target_projection_count": problem.target_projections.len(),
    })
}

fn usize_histogram(values: impl IntoIterator<Item = usize>) -> BTreeMap<String, usize> {
    let mut histogram = BTreeMap::new();
    for value in values {
        *histogram.entry(value.to_string()).or_insert(0) += 1;
    }
    histogram
}

fn keyed_count(keys: impl IntoIterator<Item = &'static str>) -> BTreeMap<&'static str, usize> {
    let mut counts = BTreeMap::new();
    for key in keys {
        *counts.entry(key).or_insert(0) += 1;
    }
    counts
}

fn variable_domain_name(domain: selector_ir::VariableDomain) -> &'static str {
    match domain {
        selector_ir::VariableDomain::Owner => "owner",
        selector_ir::VariableDomain::String => "string",
    }
}

fn claim_kind_name(claim: &ClaimKind) -> &'static str {
    match claim {
        ClaimKind::Binding { .. } => "binding",
        ClaimKind::AnonymousStatement => "anonymous_statement",
        ClaimKind::BindingGroupMember { .. } => "binding_group_member",
    }
}

fn selector_atom_kind_name(atom: &SelectorAtom) -> &'static str {
    match atom {
        SelectorAtom::OwnerKind { .. } => "owner_kind",
        SelectorAtom::OwnerDeclaresBinding { .. } => "owner_declares_binding",
        SelectorAtom::ProjectedAllowedTuples { .. } => "projected_allowed_tuples",
        SelectorAtom::OwnerExportName { .. } => "owner_export_name",
        SelectorAtom::OwnerReferencesBinding { .. } => "owner_references_binding",
        SelectorAtom::OwnerReferencesOwner { .. } => "owner_references_owner",
        SelectorAtom::OwnerAliasesOwner { .. } => "owner_aliases_owner",
        SelectorAtom::ReadsMember { .. } => "reads_member",
        SelectorAtom::ReadsMemberOfOwner { .. } => "reads_member_of_owner",
        SelectorAtom::ConsumesModuleMember { .. } => "consumes_module_member",
        SelectorAtom::PassedToCall { .. } => "passed_to_call",
        SelectorAtom::PassedToCallOfOwner { .. } => "passed_to_call_of_owner",
        SelectorAtom::MakesDecorateCall { .. } => "makes_decorate_call",
        SelectorAtom::MakesDecorateCallForOwner { .. } => "makes_decorate_call_for_owner",
        SelectorAtom::IntrinsicAlias { .. } => "intrinsic_alias",
    }
}

fn all_different_reason_name(
    reason: &selector_constraint_backend::AllDifferentReason,
) -> &'static str {
    match reason {
        selector_constraint_backend::AllDifferentReason::TargetInjectivity { .. } => {
            "target_injectivity"
        }
        selector_constraint_backend::AllDifferentReason::SelectorSemantics { .. } => {
            "selector_semantics"
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
            // The attributed constraints were not the cause: the hard ones
            // alone admit no assignment.
            Ok(no_match_result(program, result.diagnostic))
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
            let conflicts = conflict_outcomes(program, &result.conflicts)?;
            decode_satisfying_assignments(program, facts, problem, &result.assignments, &conflicts)
        }
    }
}

/// A core of one target means its own constraints admit no assignment, which
/// is a plain no-match; a larger core is a conflict among its targets.
fn conflict_outcomes<E>(
    program: &SelectorProgram,
    conflicts: &[Vec<SelectorTargetId>],
) -> Result<BTreeMap<SelectorTargetId, ClaimOutcome>, SelectorBackendSolveError<E>> {
    let mut outcomes = BTreeMap::new();
    for conflict in conflicts {
        for target in conflict {
            if !program.targets.iter().any(|known| known.id == *target) {
                return Err(SelectorBackendSolveError::UnknownConflictTarget { target: *target });
            }
            let with = conflict
                .iter()
                .copied()
                .filter(|other| other != target)
                .collect::<Vec<_>>();
            let outcome = if with.is_empty() {
                ClaimOutcome::NoMatch
            } else {
                ClaimOutcome::Conflict { with }
            };
            outcomes.insert(*target, outcome);
        }
    }
    Ok(outcomes)
}

/// Targets in `conflicts` take their outcome from it; the assignments do not
/// cover them.
fn decode_satisfying_assignments<E>(
    program: &SelectorProgram,
    facts: &SelectorFactStore,
    problem: &CompiledSelectorProblem,
    assignments: &[BackendAssignment],
    conflicts: &BTreeMap<SelectorTargetId, ClaimOutcome>,
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
            if conflicts.contains_key(&target.id) {
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
                outcome: match conflicts.get(&target.id) {
                    Some(outcome) => outcome.clone(),
                    None => {
                        claims_to_outcome(claims_by_target.remove(&target.id).unwrap_or_default())
                    }
                },
            })
            .collect(),
        global_diagnostic: None,
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

fn no_match_result(program: &SelectorProgram, diagnostic: Option<String>) -> SolverResult {
    SolverResult {
        claims: program
            .targets
            .iter()
            .map(|target| SolverClaim {
                target: target.id,
                outcome: ClaimOutcome::NoMatch,
            })
            .collect(),
        global_diagnostic: diagnostic.map(|reason| SolverGlobalDiagnostic {
            category: "unsatisfiable".to_string(),
            reason,
        }),
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
        global_diagnostic: None,
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
    use selector_constraint_backend::ConstraintValue;
    use selector_constraint_backend::{
        BackendAssignment, BackendAssignmentCoverage, BackendSolveResult, BackendSolveStatus,
        BackendValueId, BackendVariableAssignment,
    };
    use selector_constraint_model_builder::compile_selector_problem_with_summary;
    use selector_ir::{ClaimOrigin, OwnerTerm, SelectorAtom, StringTerm, VariableDomain};
    use serde_json::json;

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
                solver_response_stats: None,
                conflicts: Vec::new(),
            })
        }
    }

    /// Reports every target of the problem it receives as one conflict set.
    #[derive(Debug, Default)]
    struct ConflictingBackend {
        received: std::cell::RefCell<Vec<CompiledSelectorProblem>>,
    }

    impl SelectorProblemBackend for ConflictingBackend {
        type Error = Infallible;

        fn solve(
            &self,
            problem: &CompiledSelectorProblem,
        ) -> Result<BackendSolveResult, Self::Error> {
            self.received.borrow_mut().push(problem.clone());
            Ok(BackendSolveResult {
                status: BackendSolveStatus::Satisfiable,
                assignment_coverage: BackendAssignmentCoverage::TargetSupportComplete,
                assignments: vec![BackendAssignment { values: Vec::new() }],
                diagnostic: None,
                solver_response_stats: None,
                conflicts: vec![
                    problem
                        .target_projections
                        .iter()
                        .map(|projection| projection.target)
                        .collect(),
                ],
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

    fn duplicate_fixed_target_program() -> (SelectorProgram, SelectorTargetId, SelectorTargetId) {
        let mut program = SelectorProgram::default();
        let first_owner = program.add_variable(VariableDomain::Owner, Some("first".to_string()));
        let second_owner = program.add_variable(VariableDomain::Owner, Some("second".to_string()));
        let first_target = program.add_target(
            ChunkId(0),
            first_owner,
            "module",
            ClaimKind::Binding {
                export_name: Some("First".to_string()),
            },
            ClaimOrigin::Synthetic,
        );
        let second_target = program.add_target(
            ChunkId(0),
            second_owner,
            "module",
            ClaimKind::Binding {
                export_name: Some("Second".to_string()),
            },
            ClaimOrigin::Synthetic,
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
        facts.push(binding_fact(OwnerId(1), "minA", "Readable"));
        facts.push(owner_fact(OwnerId(2), 20, "function"));
        facts.push(binding_fact(OwnerId(2), "minB", "Readable"));
        facts
    }

    fn single_binding_facts() -> SelectorFactStore {
        let mut facts = SelectorFactStore::default();
        facts.push(owner_fact(OwnerId(1), 10, "function"));
        facts.push(binding_fact(OwnerId(1), "minA", "Readable"));
        facts
    }

    #[test]
    fn builds_backend_problem_from_selector_program() {
        let (program, target) = binding_program();
        let problem = compile_backend_problem(&program, &facts()).unwrap();

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
                .encode(&ConstraintValue::Owner(OwnerId(1)))
                .is_some()
        );
        assert!(
            problem
                .value_dictionary
                .encode(&ConstraintValue::String("minA".to_string()))
                .is_some()
        );
    }

    #[test]
    fn selector_build_summary_json_reports_input_shape_before_model_build() {
        let (program, _) = binding_program();
        let summary = selector_build_summary_json(&program, &facts(), None, None);

        assert_eq!(summary["summary_kind"], json!("selector_pre_solver"));
        assert_eq!(summary["stage"], json!("input"));
        assert_eq!(summary["selector_program"]["variable_count"], json!(2));
        assert_eq!(
            summary["selector_program"]["variable_count_by_domain"]["owner"],
            json!(1)
        );
        assert_eq!(
            summary["selector_program"]["atom_count_by_kind"]["owner_declares_binding"],
            json!(1)
        );
        assert_eq!(
            summary["selector_program"]["atom_count_by_kind"]["owner_export_name"],
            json!(1)
        );
        assert_eq!(summary["facts"]["total"], json!(4));
        assert_eq!(summary["facts"]["count_by_relation"]["owner"], json!(2));
        assert_eq!(
            summary["facts"]["count_by_relation"]["declared_binding"],
            json!(2)
        );
        assert!(summary["model_build"].is_null());
        assert!(summary["compiled_problem"].is_null());
    }

    #[test]
    fn selector_build_summary_json_reports_compiled_problem_shape() {
        let (program, _) = binding_program();
        let facts = facts();
        let compiled =
            compile_selector_problem_with_summary(&program, &facts, PresolveScope::AcrossTargets)
                .unwrap();
        let summary = selector_build_summary_json(
            &program,
            &facts,
            Some(&compiled.summary),
            Some(&compiled.problem),
        );

        assert_eq!(summary["stage"], json!("compiled_problem"));
        assert_eq!(
            summary["model_build"]["domain_value_counts"]["owner"],
            json!(2)
        );
        assert_eq!(
            summary["model_build"]["domain_value_counts"]["string"],
            json!(4)
        );
        assert_eq!(
            summary["model_build"]["stored_relation_counts"]["owner_kind"],
            json!(2)
        );
        assert_eq!(
            summary["model_build"]["stored_relation_counts"]["declared_binding"],
            json!(2)
        );
        assert_eq!(summary["compiled_problem"]["variable_count"], json!(2));
        assert_eq!(
            summary["compiled_problem"]["variable_count_by_domain"]["owner"],
            json!(1)
        );
        assert_eq!(
            summary["compiled_problem"]["variable_count_by_domain"]["string"],
            json!(1)
        );
        assert_eq!(summary["compiled_problem"]["allowed_table_count"], json!(1));
        assert_eq!(
            summary["compiled_problem"]["constraint_count_by_kind"]["allowed_table"],
            json!(1)
        );
    }

    #[test]
    fn backend_problem_preserves_constant_binding_projection() {
        let (program, target) = const_binding_program();
        let problem = compile_backend_problem(&program, &facts()).unwrap();

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
    fn hard_unsat_backend_result_maps_to_no_match() {
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

    #[test]
    fn singleton_no_constraint_problem_decodes_without_backend() {
        let (program, target) = const_binding_program();

        let result = solve_with_backend(&program, &facts(), &PanickingBackend).unwrap();

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
    fn singleton_variable_binding_projection_decodes_without_backend() {
        let (program, target) = binding_program();

        let result =
            solve_with_backend(&program, &single_binding_facts(), &PanickingBackend).unwrap();

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

    /// Presolve across targets proves the two fixed targets clash before any
    /// backend call; the program is then recompiled within targets, keeping
    /// the clash as an attributed `all_different` for the backend to localize.
    #[test]
    fn known_unsat_is_recompiled_for_conflict_localization() {
        let (program, first_target, second_target) = duplicate_fixed_target_program();
        let backend = ConflictingBackend::default();

        let result = solve_with_backend(&program, &single_binding_facts(), &backend).unwrap();

        let received = backend.received.borrow();
        let [problem] = received.as_slice() else {
            panic!("expected one backend call, got {}", received.len());
        };
        assert_eq!(problem.presolve_scope, PresolveScope::WithinTargets);
        assert_eq!(problem.known_unsat, None);
        assert_eq!(problem.all_different.len(), 1);
        assert_eq!(problem.all_different[0].variables.len(), 2);
        assert_eq!(
            result.outcome_for(first_target),
            Some(&ClaimOutcome::Conflict {
                with: vec![second_target]
            })
        );
        assert_eq!(
            result.outcome_for(second_target),
            Some(&ClaimOutcome::Conflict {
                with: vec![first_target]
            })
        );
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
                    ChunkId(0),
                    owner,
                    "module",
                    ClaimKind::Binding {
                        export_name: Some(binding.to_string()),
                    },
                    ClaimOrigin::Synthetic,
                )
            })
            .collect()
    }

    #[test]
    fn conflict_sets_decode_per_target_and_the_rest_resolves() {
        let mut program = SelectorProgram::default();
        let targets = fixed_binding_targets(&mut program, &["minA", "minB", "minC", "minD"]);
        let [left, right, lone, resolved] = targets[..] else {
            unreachable!()
        };
        let mut facts = SelectorFactStore::default();
        for (owner, binding) in [(1, "minA"), (2, "minB"), (3, "minC"), (4, "minD")] {
            facts.push(owner_fact(OwnerId(owner), owner * 10, "function"));
            facts.push(binding_fact(OwnerId(owner), binding, binding));
        }
        let problem =
            compile_selector_problem(&program, &facts, PresolveScope::WithinTargets).unwrap();
        let resolved_owner = problem
            .target_projections
            .iter()
            .find(|projection| projection.target == resolved)
            .unwrap()
            .owner_variable;

        let result = decode_backend_result::<Infallible>(
            &program,
            &facts,
            &problem,
            BackendSolveResult {
                status: BackendSolveStatus::Satisfiable,
                assignment_coverage: BackendAssignmentCoverage::TargetSupportComplete,
                assignments: vec![BackendAssignment {
                    values: vec![BackendVariableAssignment {
                        variable: resolved_owner,
                        value: backend_value_for(&problem, &owner(4)),
                    }],
                }],
                diagnostic: None,
                solver_response_stats: None,
                conflicts: vec![vec![left, right], vec![lone]],
            },
        )
        .unwrap();

        assert_eq!(
            result.outcome_for(left),
            Some(&ClaimOutcome::Conflict { with: vec![right] })
        );
        assert_eq!(
            result.outcome_for(right),
            Some(&ClaimOutcome::Conflict { with: vec![left] })
        );
        // A core of one target is its own constraints failing: a no-match.
        assert_eq!(result.outcome_for(lone), Some(&ClaimOutcome::NoMatch));
        assert_eq!(
            result.outcome_for(resolved),
            Some(&ClaimOutcome::Unique {
                claim: ResolvedClaim {
                    chunk_id: ChunkId(0),
                    owner: OwnerId(4),
                    statement_ordinal: StatementOrdinal(40),
                    binding: Some("minD".to_string()),
                    provenance: Vec::new(),
                }
            })
        );
    }

    /// Within targets, a target's own candidate restriction that leaves no
    /// value becomes an empty table attributed to that target alone, instead
    /// of an empty domain that would fail the whole program.
    #[test]
    fn within_targets_exhausted_domain_becomes_an_attributed_empty_table() {
        let mut program = SelectorProgram::default();
        let targets = fixed_binding_targets(&mut program, &["absent", "minA"]);
        let problem = compile_selector_problem(
            &program,
            &single_binding_facts(),
            PresolveScope::WithinTargets,
        )
        .unwrap();

        assert_eq!(problem.known_unsat, None);
        let absent_owner = problem
            .target_projections
            .iter()
            .find(|projection| projection.target == targets[0])
            .unwrap()
            .owner_variable;
        assert!(
            problem.allowed_tuples.iter().any(|constraint| {
                constraint.variables == [absent_owner]
                    && problem.allowed_tuple_rows(constraint).is_empty()
            }),
            "{problem:#?}"
        );
    }
}
