use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use anyhow::{Result, bail};
use rayon::prelude::*;
use serde::Serialize;
use swc_common::GLOBALS;

use analysis::facts::ExportReentryPolicy;
use analysis::{DependencyGraphOptions, RedundantPurityHint};

use artifact::paths::normalize_module_path;
use artifact::reports::{SelectedModuleLowering, StaticModuleRequest, StaticModuleRequestMode};
use artifact::{ArtifactIndexes, ChunkBundle, ChunkId};

use output_layout::MODULES_REPORT;
use reconstruction::UnmatchedSpecClaim;

use spec::{ChunkExportPurity, ChunkRenames, LogicalModule, UnassignedMode};

mod anonymous;
mod body_facts;
mod chunk_ast;
mod chunk_renames;
mod cross_module;
mod exports;
mod imports;
mod io;
mod lower;
mod materialize;
mod module_output;
mod naturalize;
mod placement;
mod plans;
mod rebind_fold;
pub mod rename_ledger;
#[cfg(test)]
mod rename_ledger_proptest;
mod rewrite_runtime;
mod scope_names;
mod util;
mod visitors;

pub use imports::naturalize_cross_chunk_imports;

use imports::VendorReimportOracle;
use io::{prepare_output_dir, prune_artifact_to_chunk_ids, write_chunk_report_json};

use materialize::{
    ChunkContext, ChunkSpec, EmissionChunkOutputs, MaterializeLogicalChunkInputs,
    claim_prepared_chunk, collect_materialized_logical_chunks, finish_logical_chunk,
    prepare_logical_chunk,
};
use spec_resolution::solve_projected_chunks;

use util::normalize_optional_relative_dir;

pub struct MaterializeLogicalModulesResult {
    /// Finalized lowered and pass-through files, kept outside a bundle until
    /// the remaining post-lowering passes need their legacy input.
    pub output: EmissionChunkOutputs,
    pub selected_lowerings: Vec<SelectedModuleLowering>,
    pub module_count: usize,
    /// Per-symbol counts of vendor-swap rewrites applied at
    /// construction time in materialized module bodies, keyed by
    /// (swapped chunk, chunk export). Folded into the vendor waves'
    /// `references_rewritten` manifest fields by the pipeline so the
    /// manifest keeps counting emitted references across both
    /// application sites.
    pub vendor_reference_rewrites: BTreeMap<(ChunkId, String), usize>,
    /// Spec claims that named a binding for which no top-level
    /// declaration exists in the source chunk. Previously dropped
    /// silently — the binding would fall through to the residual and
    /// the named module's export list would be one entry short. Now
    /// collected across every chunk so reconstruction can diagnose
    /// all claims (the chunk lowers as if the missing claim were absent).
    /// Final validation rejects the full list before writing the output. See
    /// [`UnmatchedSpecClaim`].
    pub unmatched_spec_claims: Vec<UnmatchedSpecClaim>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChunkModulesReport {
    pub chunk_id: String,
    pub counts: ChunkModulesCounts,
    pub final_module_contents: Vec<FinalModuleContent>,
    pub requested_logical_modules: Vec<RequestedLogicalModule>,
    /// `purity: pure` hints the analyzer inferred automatically.
    /// Same content as the stderr warnings the build prints; carried
    /// on the report so JSON consumers can pin behavior across runs.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub redundant_purity_hints: Vec<RedundantPurityHint>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChunkModulesCounts {
    pub applied: usize,
    pub selected_bindings: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct FinalModuleContent {
    pub binding_names: Vec<String>,
    pub file: String,
    pub member_names: Vec<String>,
    /// Canonical [`spec::ModulePath`] (the report is per-chunk, so
    /// the chunk is implicit).
    pub path: spec::ModulePath,
    pub source_unit_ids: Vec<String>,
    pub residual: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct RequestedLogicalModule {
    /// Canonical [`spec::ModulePath`] of the requested target.
    pub target_path: spec::ModulePath,
    pub residual: bool,
}

/// Why the unknown-call fallback includes the chunk's export surface.
/// The policy remains conservative whenever either blocker list is non-empty.
#[derive(Debug, Clone, Serialize)]
pub struct ExportReentryReport {
    pub execution_runtime: spec::ExecutionRuntime,
    pub policy: ExportReentryPolicy,
    pub global_blockers: Vec<ExportReentryGlobalBlocker>,
    pub closure_blockers: Vec<ExportReentryClosureBlocker>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "reason", rename_all = "snake_case")]
pub enum ExportReentryGlobalBlocker {
    IncompleteArtifact {
        present_chunks: usize,
        expected_chunks: usize,
    },
    CommonJsInterop {
        source_chunk: String,
    },
    NonOrdinaryStaticRequest {
        source_chunk: String,
        specifier: String,
        resolved_target: Option<ExportReentryTarget>,
    },
}

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "reason", rename_all = "snake_case")]
pub enum ExportReentryClosureBlocker {
    NonEsmChunk {
        source_chunk: String,
    },
    TopLevelAwait {
        source_chunk: String,
    },
    CommonJsInterop {
        source_chunk: String,
    },
    VendorAffectedChunk {
        source_chunk: String,
    },
    UnsupportedStaticRequest {
        source_chunk: String,
        specifier: String,
        resolved_target: Option<ExportReentryTarget>,
    },
    UnresolvedStaticRequest {
        source_chunk: String,
        specifier: String,
    },
    MissingTargetChunk {
        source_chunk: String,
        specifier: String,
        target_chunk: String,
    },
    NonEntryStaticRequest {
        source_chunk: String,
        specifier: String,
        resolved_target: ExportReentryTarget,
        entry_file: String,
    },
    TargetCycle {
        target_chunk: String,
    },
}

#[derive(Debug, Clone, Serialize)]
pub struct ExportReentryTarget {
    pub chunk: String,
    pub file: String,
}

/// Where (and when) per-chunk validation reports are written.
#[derive(Debug, Clone)]
pub enum ReportEmission {
    /// No reports.
    None,
    /// Write every per-chunk report (dependency graph, modules report,
    /// rejection evidence) under this root. The directory is prepared
    /// (must be empty) up front.
    Full(PathBuf),
    /// Write reports only when the realizability gate rejects. Used by
    /// `debundle run --dry-run`: the accept path writes nothing (the
    /// no-output contract), but a rejection still materializes
    /// `dependency_graph.json` + `cycles.json` / `atomic_group_conflicts.json`
    /// so the documented `debundle gate list/describe` follow-up works.
    OnRejection(PathBuf),
}

impl ReportEmission {
    /// Root for accept-path reports (dependency graph, modules report).
    pub fn full_dir(&self) -> Option<&Path> {
        match self {
            Self::Full(dir) => Some(dir),
            Self::None | Self::OnRejection(_) => None,
        }
    }

    /// Root for rejection evidence (written on gate rejection).
    pub fn rejection_dir(&self) -> Option<&Path> {
        match self {
            Self::Full(dir) | Self::OnRejection(dir) => Some(dir),
            Self::None => None,
        }
    }
}

#[derive(Debug, Clone)]
pub struct MaterializeLogicalModulesOptions {
    pub config: spec::MaterializeLogicalModulesConfig,
    pub chunk_ids: Vec<String>,
    pub keep_going: bool,
    /// See `pipeline::TransformRunOptions::list_template_identifiers`.
    pub list_template_identifiers: bool,
    pub report_emission: ReportEmission,
}

/// Spec-derived inputs to [`materialize_logical_modules`], all keyed by
/// chunk id.
pub struct MaterializeSpecInputs<'a> {
    pub execution_runtime: spec::ExecutionRuntime,
    pub logical_modules: &'a BTreeMap<String, BTreeMap<String, LogicalModule>>,
    pub chunk_renames: &'a BTreeMap<String, ChunkRenames>,
    pub unassigned_mode: &'a BTreeMap<String, UnassignedMode>,
    pub chunk_analysis_options: &'a BTreeMap<String, DependencyGraphOptions>,
    pub chunk_export_purity: &'a BTreeMap<String, ChunkExportPurity>,
}

pub fn materialize_logical_modules(
    mut artifact: ChunkBundle,
    spec_inputs: MaterializeSpecInputs<'_>,
    vendor_plan: &vendor::VendorResolutionPlan,
    options: MaterializeLogicalModulesOptions,
) -> Result<MaterializeLogicalModulesResult> {
    let MaterializeSpecInputs {
        execution_runtime,
        logical_modules,
        chunk_renames,
        unassigned_mode,
        chunk_analysis_options,
        chunk_export_purity,
    } = spec_inputs;
    if options.chunk_ids.is_empty() {
        bail!("materialize_logical_modules requires at least one chunk_id");
    }
    let target_dir = normalize_optional_relative_dir(&options.config.target_dir)?;
    let mut selected_chunk_ids = Vec::new();
    let mut seen = BTreeSet::new();
    for chunk_id in &options.chunk_ids {
        let normalized = normalize_module_path(chunk_id)?;
        if seen.insert(normalized.clone()) {
            selected_chunk_ids.push(normalized);
        }
    }

    if let Some(dir) = options.report_emission.full_dir() {
        prepare_output_dir(dir)?;
    }

    if options.config.prune_other_chunks {
        prune_artifact_to_chunk_ids(&mut artifact, &selected_chunk_ids);
    }
    let artifact_indexes = ArtifactIndexes::build(&artifact)?;
    let vendor_affected_chunk_ids = vendor_plan.affected_chunk_ids();
    let export_reentry_reports = export_reentry_reports(
        &artifact,
        &artifact_indexes,
        execution_runtime,
        vendor_plan,
        &vendor_affected_chunk_ids,
        &selected_chunk_ids,
        options.report_emission.rejection_dir().is_some(),
    )?;
    let export_reentry_policies = export_reentry_reports
        .iter()
        .map(|(chunk_id, report)| (*chunk_id, report.policy))
        .collect();
    // Program-level pass over the selected chunks' retained static-import
    // closure: cross-module purity verdicts for each chunk's imported
    // function bindings, consumed per chunk via
    // `AnalysisInputs::imported_purities`.
    // Fully-swapped vendor chunks stay opaque — they are excluded from
    // the emission set and their bodies are replaced by the upstream
    // package, so they must not contribute purity facts.
    let full_swap_chunk_ids = vendor_plan.full_swap_chunk_ids();
    let cross_module_chunk_closure = cross_module::selected_static_import_closure(
        &artifact,
        &artifact_indexes,
        &selected_chunk_ids,
        &full_swap_chunk_ids,
    )?;
    let artifact_ref: &ChunkBundle = &artifact;
    // Vendor consultation for lowering's import construction: runtime re-imports whose source
    // directive targets a partially / bundled-partially swapped chunk
    // are constructed as package / facade imports from the plan oracle
    // instead of chunk re-imports the post-materialize wave would
    // rewrite. `None` when no vendor mark could affect construction.
    let vendor_import_oracle =
        VendorReimportOracle::new(vendor_plan, &artifact.chunk_table, &artifact_indexes);
    // SWC's `swc_common::GLOBALS` is a `scoped_tls` thread-local, so the
    // outer `GLOBALS.set` wrap in `main.rs` / `run_agent` does NOT carry
    // into rayon worker threads. Capture a reference to the current
    // `Globals` and re-set inside each worker closure so `Mark::new()`
    // and `Id`-comparisons stay consistent across the whole pipeline.
    //
    // Chunks are planned in parallel up to the selector resolve, resolved
    // together, then analysed and lowered in parallel. Each phase's errors
    // are taken in chunk-id order, so which one fails a run (under
    // `--fail-fast`, which selector outcome) does not depend on scheduling.
    let prepared = GLOBALS
        .with(|globals| {
            selected_chunk_ids
                .par_iter()
                .map(|chunk_id| {
                    GLOBALS.set(globals, || {
                        prepare_logical_chunk(MaterializeLogicalChunkInputs {
                            context: ChunkContext {
                                artifact: artifact_ref,
                                artifact_indexes: &artifact_indexes,
                                chunk_id,
                                file: options.config.file.as_deref(),
                                target_dir: &target_dir,
                                keep_going: options.keep_going,
                                list_template_identifiers: options.list_template_identifiers,
                                report_emission: &options.report_emission,
                                export_reentry_reports: &export_reentry_reports,
                                vendor_import_oracle: vendor_import_oracle.as_ref(),
                                export_reentry_policies: &export_reentry_policies,
                            },
                            spec: ChunkSpec {
                                logical_modules,
                                chunk_renames,
                                unassigned_mode,
                                chunk_analysis_options,
                            },
                        })
                    })
                })
                .collect::<Vec<_>>()
        })
        .into_iter()
        .collect::<Result<Vec<_>>>()?;
    let (prepared, projections): (Vec<_>, Vec<_>) = prepared.into_iter().unzip();
    let resolutions = solve_projected_chunks(projections)?;
    let claimed = GLOBALS
        .with(|globals| {
            prepared
                .into_par_iter()
                .zip(resolutions)
                .map(|(chunk, resolution)| {
                    GLOBALS.set(globals, || claim_prepared_chunk(chunk, resolution))
                })
                .collect::<Vec<_>>()
        })
        .into_iter()
        .collect::<Result<Vec<_>>>()?;
    let declared_pure_new = claimed
        .iter()
        .map(|chunk| {
            let (chunk_id, bindings) = chunk.declared_pure_new();
            (chunk_id.to_string(), bindings.clone())
        })
        .collect();
    let cross_module_purities = cross_module::collect_cross_module_imported_purities(
        &artifact,
        &artifact_indexes,
        chunk_export_purity,
        &declared_pure_new,
        &full_swap_chunk_ids,
        Some(&cross_module_chunk_closure),
    );
    let chunk_results = GLOBALS
        .with(|globals| {
            claimed
                .into_par_iter()
                .map(|chunk| {
                    GLOBALS.set(globals, || {
                        finish_logical_chunk(chunk, &cross_module_purities)
                    })
                })
                .collect::<Vec<_>>()
        })
        .into_iter()
        .collect::<Result<Vec<_>>>()?;

    let mut reports = Vec::with_capacity(chunk_results.len());
    let mut applied = Vec::<SelectedModuleLowering>::new();
    let mut unmatched_spec_claims = Vec::<UnmatchedSpecClaim>::new();
    let mut vendor_reference_rewrites = BTreeMap::<(ChunkId, String), usize>::new();
    for chunk_result in &chunk_results {
        if let Some(report_out_dir) = options.report_emission.full_dir() {
            write_chunk_report_json(
                report_out_dir,
                artifact.chunk_table.name(chunk_result.chunk_id),
                MODULES_REPORT,
                &chunk_result.report,
            )?;
        }
        applied.extend(chunk_result.applied.iter().cloned());
        reports.push(chunk_result.report.clone());
        unmatched_spec_claims.extend(chunk_result.unmatched_spec_claims.iter().cloned());
        for (key, count) in &chunk_result.vendor_reference_rewrites {
            *vendor_reference_rewrites.entry(key.clone()).or_insert(0) += count;
        }
    }
    let output = collect_materialized_logical_chunks(artifact, &target_dir, chunk_results)?;

    let module_count: usize = reports.iter().map(|r| r.final_module_contents.len()).sum();
    Ok(MaterializeLogicalModulesResult {
        output,
        selected_lowerings: applied,
        module_count,
        vendor_reference_rewrites,
        unmatched_spec_claims,
    })
}

fn export_reentry_reports(
    artifact: &ChunkBundle,
    indexes: &ArtifactIndexes,
    execution_runtime: spec::ExecutionRuntime,
    vendor_plan: &vendor::VendorResolutionPlan,
    vendor_affected_chunk_ids: &BTreeSet<ChunkId>,
    selected_chunk_names: &[String],
    include_closure_diagnostics: bool,
) -> Result<BTreeMap<ChunkId, ExportReentryReport>> {
    // An incomplete artifact cannot certify incoming host/nonordinary edges.
    let all_chunks_present = artifact.chunks.len() == artifact.chunk_table.len();
    let mut global_blockers = Vec::new();
    if !all_chunks_present {
        global_blockers.push(ExportReentryGlobalBlocker::IncompleteArtifact {
            present_chunks: artifact.chunks.len(),
            expected_chunks: artifact.chunk_table.len(),
        });
    }
    for chunk in &artifact.chunks {
        let source_chunk = artifact.chunk_table.name(chunk.chunk_id).to_string();
        let runtime = &chunk.analysis.runtime_facts;
        if runtime.has_cjs_interop && execution_runtime == spec::ExecutionRuntime::Generic {
            global_blockers.push(ExportReentryGlobalBlocker::CommonJsInterop {
                source_chunk: source_chunk.clone(),
            });
        }
        for request in &runtime.static_requests {
            if request.mode != StaticModuleRequestMode::OrdinaryEsm {
                global_blockers.push(ExportReentryGlobalBlocker::NonOrdinaryStaticRequest {
                    source_chunk: source_chunk.clone(),
                    specifier: request.specifier.clone(),
                    resolved_target: resolve_report_target(
                        request,
                        chunk.chunk_id,
                        &chunk.analysis.entry_file,
                        artifact,
                        indexes,
                    ),
                });
            }
        }
    }

    let global_proof_is_blocked = !global_blockers.is_empty();
    let mut reports = BTreeMap::new();
    for name in selected_chunk_names {
        let Some(target) = artifact.chunk_table.get(name) else {
            continue;
        };
        let closure_blockers = if artifact.has_chunk(target)
            && (include_closure_diagnostics || !global_proof_is_blocked)
        {
            collect_closure_blockers(
                target,
                artifact,
                indexes,
                execution_runtime,
                vendor_plan,
                vendor_affected_chunk_ids,
            )?
        } else {
            Vec::new()
        };
        let policy = if !global_proof_is_blocked && closure_blockers.is_empty() {
            ExportReentryPolicy::NoStaticImporterReentry
        } else {
            ExportReentryPolicy::Conservative
        };
        reports.insert(
            target,
            ExportReentryReport {
                execution_runtime,
                policy,
                global_blockers: global_blockers.clone(),
                closure_blockers,
            },
        );
    }
    Ok(reports)
}

fn collect_closure_blockers(
    target: ChunkId,
    artifact: &ChunkBundle,
    indexes: &ArtifactIndexes,
    execution_runtime: spec::ExecutionRuntime,
    vendor_plan: &vendor::VendorResolutionPlan,
    vendor_affected_chunk_ids: &BTreeSet<ChunkId>,
) -> Result<Vec<ExportReentryClosureBlocker>> {
    // Follow resolved static requests even when another blocker is present,
    // so the report exposes independent predicates and structural cycles.
    let mut outgoing = BTreeMap::<ChunkId, BTreeSet<ChunkId>>::new();
    let mut closure = BTreeSet::from([target]);
    let mut pending = vec![target];
    let mut blockers = Vec::new();
    while let Some(source) = pending.pop() {
        let chunk = artifact.chunk(source)?;
        let runtime = &chunk.analysis.runtime_facts;
        let source_name = artifact.chunk_table.name(source).to_string();
        if runtime.module_kind != artifact::reports::ChunkModuleKind::Esm {
            blockers.push(ExportReentryClosureBlocker::NonEsmChunk {
                source_chunk: source_name.clone(),
            });
        }
        if runtime.has_top_level_await {
            blockers.push(ExportReentryClosureBlocker::TopLevelAwait {
                source_chunk: source_name.clone(),
            });
        }
        if runtime.has_cjs_interop && execution_runtime == spec::ExecutionRuntime::Generic {
            blockers.push(ExportReentryClosureBlocker::CommonJsInterop {
                source_chunk: source_name.clone(),
            });
        }
        if vendor_affected_chunk_ids.contains(&source)
            && (execution_runtime == spec::ExecutionRuntime::Generic
                || !vendor_plan.preserves_static_import_closure(source))
        {
            blockers.push(ExportReentryClosureBlocker::VendorAffectedChunk {
                source_chunk: source_name.clone(),
            });
        }
        for request in &runtime.static_requests {
            if request.mode != StaticModuleRequestMode::OrdinaryEsm {
                blockers.push(ExportReentryClosureBlocker::UnsupportedStaticRequest {
                    source_chunk: source_name.clone(),
                    specifier: request.specifier.clone(),
                    resolved_target: resolve_report_target(
                        request,
                        source,
                        &chunk.analysis.entry_file,
                        artifact,
                        indexes,
                    ),
                });
            }
            let Some(resolved) = indexes.resolve_runtime_import_reference(
                &request.specifier,
                source,
                &chunk.analysis.entry_file,
                &artifact.chunk_table,
            ) else {
                blockers.push(ExportReentryClosureBlocker::UnresolvedStaticRequest {
                    source_chunk: source_name.clone(),
                    specifier: request.specifier.clone(),
                });
                continue;
            };
            if !artifact.has_chunk(resolved.target_chunk_id) {
                blockers.push(ExportReentryClosureBlocker::MissingTargetChunk {
                    source_chunk: source_name.clone(),
                    specifier: request.specifier.clone(),
                    target_chunk: artifact
                        .chunk_table
                        .name(resolved.target_chunk_id)
                        .to_string(),
                });
                continue;
            }
            let target_entry = &artifact
                .chunk(resolved.target_chunk_id)?
                .analysis
                .entry_file;
            if target_entry != &resolved.target_file {
                blockers.push(ExportReentryClosureBlocker::NonEntryStaticRequest {
                    source_chunk: source_name.clone(),
                    specifier: request.specifier.clone(),
                    resolved_target: report_target(
                        resolved.target_chunk_id,
                        &resolved.target_file,
                        artifact,
                    ),
                    entry_file: target_entry.clone(),
                });
                continue;
            }
            outgoing
                .entry(source)
                .or_default()
                .insert(resolved.target_chunk_id);
            if closure.insert(resolved.target_chunk_id) {
                pending.push(resolved.target_chunk_id);
            }
        }
    }

    let mut incoming = BTreeMap::<ChunkId, BTreeSet<ChunkId>>::new();
    for (source, targets) in &outgoing {
        for destination in targets {
            incoming.entry(*destination).or_default().insert(*source);
        }
    }
    let mut reaches_target = BTreeSet::from([target]);
    let mut pending = vec![target];
    while let Some(destination) = pending.pop() {
        if let Some(sources) = incoming.get(&destination) {
            for source in sources {
                if reaches_target.insert(*source) {
                    pending.push(*source);
                }
            }
        }
    }
    let target_has_cycle = outgoing.get(&target).is_some_and(|destinations| {
        destinations
            .iter()
            .any(|destination| reaches_target.contains(destination))
    });
    if target_has_cycle {
        blockers.push(ExportReentryClosureBlocker::TargetCycle {
            target_chunk: artifact.chunk_table.name(target).to_string(),
        });
    }
    Ok(blockers)
}

fn resolve_report_target(
    request: &StaticModuleRequest,
    source: ChunkId,
    entry_file: &str,
    artifact: &ChunkBundle,
    indexes: &ArtifactIndexes,
) -> Option<ExportReentryTarget> {
    let resolved = indexes.resolve_runtime_import_reference(
        &request.specifier,
        source,
        entry_file,
        &artifact.chunk_table,
    )?;
    Some(report_target(
        resolved.target_chunk_id,
        &resolved.target_file,
        artifact,
    ))
}

fn report_target(target: ChunkId, file: &str, artifact: &ChunkBundle) -> ExportReentryTarget {
    ExportReentryTarget {
        chunk: artifact.chunk_table.name(target).to_string(),
        file: file.to_string(),
    }
}
