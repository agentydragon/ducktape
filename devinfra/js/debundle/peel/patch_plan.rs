//! Patch-plan report: whether each emitted module's claims cover whole atomic
//! groups.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use anonymous_resolution::{SourceClaimSet, resolve_source_claims};
use anyhow::Result;
use clap::Args as ClapArgs;
use serde::Serialize;

use graph::DependencyGraphInput;
use graph::report::{AtomicGroupReport, DependencyGraphReport};
use graph_provenance::VerifiedGraphSourceContext;
use spec_modules::{collect_module_files, module_path_from_file, read_module_claims};

use crate::output_format::OutputFormat;
use crate::plan::{CommonArgs, LimitReport};
use crate::selection::{ResolvedSelection, binding_to_source_units, resolve_claimed_units};

#[derive(Debug, Clone, ClapArgs)]
pub struct PatchPlanArgs {
    #[command(flatten)]
    pub common: CommonArgs,

    /// Maximum number of rows to keep. Zero means unlimited.
    #[arg(long, default_value_t = 0)]
    pub limit: usize,

    /// Root used to resolve relative `source_location.source_path`
    /// values when coverage checks anonymous statement selectors.
    #[arg(long = "source-root", env = "DEBUNDLE_SOURCE_ROOT")]
    pub source_root: Option<PathBuf>,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct PatchPlanReport {
    pub rows: Vec<PatchPlanRow>,
    pub summary: PatchPlanSummary,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub limits: Option<LimitReport>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct PatchPlanSummary {
    pub total_patch_sets: usize,
    pub complete_patch_sets: usize,
    pub split_patch_sets: usize,
    pub unknown_binding_count: usize,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct PatchPlanRow {
    pub path: String,
    pub file: String,
    pub status: PatchPlanStatus,
    pub requested_binding_ids: Vec<String>,
    pub unknown_binding_ids: Vec<String>,
    pub atomic_group_ids: Vec<String>,
    pub complete_atomic_group_ids: Vec<String>,
    pub split_atomic_group_ids: Vec<String>,
    pub missing_binding_ids: Vec<String>,
    pub missing_source_unit_ids: Vec<String>,
    pub missing_anonymous_source_unit_ids: Vec<String>,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "snake_case")]
pub enum PatchPlanStatus {
    CompleteAtomicGroups,
    SplitAtomicGroups,
    UnknownBindings,
}

pub fn run_patch_plan_report(
    args: &PatchPlanArgs,
    source_context: Option<&VerifiedGraphSourceContext>,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<PatchPlanReport> {
    let input = DependencyGraphInput::read(&args.common.dependency_graph_path)?;
    let graph = input.report();
    let mut rows = patch_plan_rows(
        graph,
        &args.common.dependency_graph_path,
        &args.common.modules_root,
        args.source_root.as_deref(),
        source_context,
        diagnostics,
    )?;
    rows.sort_by_key(|row| (row.status, row.path.clone()));
    let summary = PatchPlanSummary {
        total_patch_sets: rows.len(),
        complete_patch_sets: rows
            .iter()
            .filter(|row| row.status == PatchPlanStatus::CompleteAtomicGroups)
            .count(),
        split_patch_sets: rows
            .iter()
            .filter(|row| row.status == PatchPlanStatus::SplitAtomicGroups)
            .count(),
        unknown_binding_count: rows.iter().map(|row| row.unknown_binding_ids.len()).sum(),
    };
    let mut limits = LimitReport::new(args.limit);
    limits.apply("rows", &mut rows);
    Ok(PatchPlanReport {
        rows,
        summary,
        limits: (args.limit > 0).then_some(limits),
    })
}

fn patch_plan_rows(
    graph: &DependencyGraphReport,
    dependency_graph_path: &Path,
    modules_root: &Path,
    source_root: Option<&Path>,
    source_context: Option<&VerifiedGraphSourceContext>,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<Vec<PatchPlanRow>> {
    let binding_to_source_units = binding_to_source_units(graph);
    let atomic_group_by_source_unit = atomic_group_by_source_unit(graph);
    let atomic_group_by_id: BTreeMap<String, &AtomicGroupReport> = graph
        .atomic_graph
        .nodes
        .iter()
        .map(|atomic_group| (atomic_group.id.clone(), atomic_group))
        .collect();
    let patch_sets = load_patch_sets(modules_root)?;
    // Resolve every patch set's source-backed claims in ONE batched call: the
    // resolver parses each claimed source file once per call.
    let resolved_by_set = resolve_source_claims(
        anonymous_resolution::SourceClaimsContext {
            graph,
            dependency_graph_path,
            modules_root,
            source_root,
            source_context,
            diagnostics,
        },
        &patch_sets
            .iter()
            .map(|patch_set| SourceClaimSet {
                module_path: &patch_set.file,
                module: &patch_set.claims,
            })
            .collect::<Vec<_>>(),
    )?;
    patch_sets
        .into_iter()
        .zip(resolved_by_set)
        .map(|(patch_set, resolved)| {
            let claimed_bindings: BTreeSet<String> = patch_set
                .claims
                .binding_pins()
                .map(str::to_owned)
                .chain(resolved.bindings)
                .collect();
            let ResolvedSelection {
                source_unit_ids: requested_source_unit_ids,
                unknown_binding_ids,
            } = resolve_claimed_units(
                graph,
                &binding_to_source_units,
                claimed_bindings.iter().map(String::as_str),
                &resolved.anonymous_source_units,
            );

            let mut atomic_group_ids: BTreeSet<String> = BTreeSet::new();
            for source_unit_id in &requested_source_unit_ids {
                if let Some(atomic_group_id) = atomic_group_by_source_unit.get(source_unit_id) {
                    atomic_group_ids.insert(atomic_group_id.clone());
                }
            }

            let mut complete_atomic_group_ids = Vec::<String>::new();
            let mut split_atomic_group_ids = Vec::<String>::new();
            let mut missing_binding_ids = BTreeSet::<String>::new();
            let mut missing_source_unit_ids = BTreeSet::<String>::new();
            let mut missing_anonymous_source_unit_ids = BTreeSet::<String>::new();
            for atomic_group_id in &atomic_group_ids {
                let Some(atomic_group) = atomic_group_by_id.get(atomic_group_id) else {
                    continue;
                };
                let missing_bindings: Vec<_> = atomic_group
                    .members
                    .iter()
                    .map(|member| member.binding.as_str())
                    .filter(|binding| !claimed_bindings.contains(*binding))
                    .collect();
                let missing_units: Vec<_> = atomic_group
                    .source_unit_ids
                    .iter()
                    .filter(|id| !requested_source_unit_ids.contains(*id))
                    .collect();
                if missing_bindings.is_empty() && missing_units.is_empty() {
                    complete_atomic_group_ids.push(atomic_group_id.clone());
                } else {
                    split_atomic_group_ids.push(atomic_group_id.clone());
                    missing_binding_ids.extend(missing_bindings.into_iter().map(str::to_owned));
                    missing_source_unit_ids.extend(missing_units.into_iter().cloned());
                    missing_anonymous_source_unit_ids.extend(
                        atomic_group
                            .anonymous_statement_source_unit_ids
                            .iter()
                            .filter(|source_unit_id| {
                                !requested_source_unit_ids.contains(*source_unit_id)
                            })
                            .cloned(),
                    );
                }
            }
            let status = if !split_atomic_group_ids.is_empty() {
                PatchPlanStatus::SplitAtomicGroups
            } else if !unknown_binding_ids.is_empty() {
                PatchPlanStatus::UnknownBindings
            } else {
                PatchPlanStatus::CompleteAtomicGroups
            };
            Ok(PatchPlanRow {
                path: patch_set.path,
                file: patch_set.file.display().to_string(),
                status,
                requested_binding_ids: claimed_bindings.into_iter().collect(),
                unknown_binding_ids,
                atomic_group_ids: atomic_group_ids.into_iter().collect(),
                complete_atomic_group_ids,
                split_atomic_group_ids,
                missing_binding_ids: missing_binding_ids.into_iter().collect(),
                missing_source_unit_ids: missing_source_unit_ids.into_iter().collect(),
                missing_anonymous_source_unit_ids: missing_anonymous_source_unit_ids
                    .into_iter()
                    .collect(),
            })
        })
        .collect()
}

#[derive(Debug, Clone)]
struct PatchSet {
    path: String,
    file: PathBuf,
    claims: spec_modules::ModuleClaims,
}

fn load_patch_sets(modules_root: &Path) -> Result<Vec<PatchSet>> {
    let mut sets = Vec::<PatchSet>::new();
    for file in collect_module_files(modules_root)? {
        let claims = read_module_claims(&file)?;
        if claims.is_empty() {
            continue;
        }
        sets.push(PatchSet {
            path: module_path_from_file(&file, modules_root),
            file,
            claims,
        });
    }
    Ok(sets)
}

fn atomic_group_by_source_unit(graph: &DependencyGraphReport) -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    for atomic_group in &graph.atomic_graph.nodes {
        for source_unit_id in &atomic_group.source_unit_ids {
            out.insert(source_unit_id.clone(), atomic_group.id.clone());
        }
    }
    out
}
