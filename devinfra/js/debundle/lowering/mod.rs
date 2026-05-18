use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fs;
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use anyhow::{Context, Result, bail};
use rayon::prelude::*;
use serde::Serialize;
use swc_common::{DUMMY_SP, EqIgnoreSpan, GLOBALS, SyntaxContext};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitMut, VisitMutWith, VisitWith};

use analysis::{
    AnalysisHints, AtomicUnitConflict, BindingKind, BindingName, ChunkFactorization, DepKind,
    KnownEffect, LogicalModule as FactorizationLogicalModule, LogicalModuleIndex, ModuleId,
    OwnerGraphAndUnits, OwnerId, RedundantPureMemberReason, RedundantPurityHint,
    RedundantPurityReason, analyze_chunk, compute_owner_graph_and_units,
    render_atomic_unit_conflict_summary, render_cycle_summary, top_level_id,
};
use artifact::{
    ArtifactIndexes, ArtifactSourceImportResolver, ChunkAnalysis, ChunkArtifact, ChunkBundle,
    ChunkDecompositionOutput, ChunkFileRecord, ChunkId, ChunkLogicalModulesSummary, ChunkMetadata,
    ChunkTable, FileMetadata, FileRole, JsChunk, JsFile, JsFileBody, SelectedModuleLowering,
    get_chunk_entry_path, join_module_path, manifest_relative_path, module_path_dirname,
    module_path_from_path, normalize_module_path, normalize_relative_module_specifier,
    relative_module_path,
};
use js_ast::{ParsedJsModule, set_str_value, str_value};
use spec::{
    BindingSourceKind, ChunkRenames, LogicalModule, MemberEffect, MemberPurity, UnassignedMode,
};

mod anonymous;
mod body_facts;
mod chunk_ast;
mod chunk_renames;
mod exports;
mod imports_cross;
mod imports_runtime;
mod lower;
mod materialize;
mod naturalize;
mod plan_references;
mod plans;
mod rewrite_runtime;
mod runtime_imports;
mod util;
mod visitors;

use anonymous::resolve_anonymous_statement_ordinals;
use body_facts::{ModuleBodyFacts, RefCollector, collect_module_body_facts};
use chunk_ast::{
    ChunkAstAnalysis, TopLevelDecl, analyze_chunk_ast, binding_ids, binding_names, declaration_ids,
    declaration_names, top_level_declaration_ids,
};
use chunk_renames::collect_chunk_renames;
use exports::{
    auto_grown_residual_exports, entry_exports_for_moved_bindings, export_named_for_bindings,
    reject_duplicate_export_names, reject_duplicate_member_bindings, trim_dead_named_specifiers,
};
use imports_cross::{
    collect_entry_exports_by_original_local, cross_module_imports_for_plan, final_module_exports,
    residual_entry_imports_for_moved_body,
};
use imports_runtime::{
    import_decl_module_item, resolve_imported_binding, source_chunk_imports_for_moved_body,
};
use lower::{LowerChunkInputs, LoweredChunk, lower_chunk};
use materialize::{
    MaterializeLogicalChunkInputs, aggregate_logical_timings, apply_materialized_logical_chunks,
    materialize_logical_chunk,
};
use naturalize::naturalize_module_body;
use plan_references::{
    ArtifactSourceImportResolutionCache, EntryExport, ModuleReferenceNeeds, RuntimeImportLookup,
    collect_imported_reexports_by_module, plan_module_reference_needs,
};
use plans::{
    LogicalRequest, MemberRequest, ModulePlan, known_effect_from_member_effect,
    logical_requests_for_chunk, synthesize_mini_factor_plans,
};
use rewrite_runtime::rewrite_runtime_sources_for_target;
use runtime_imports::{
    RuntimeImportFacts, RuntimeImportInfo, RuntimeImportKind, imported_binding_named_specifier,
    record_runtime_imports, runtime_reimport_specifier,
};
use util::{
    normalize_optional_relative_dir, prepare_output_dir, prune_artifact_to_chunk_ids,
    write_chunk_report_json,
};
use visitors::{IdentifierRenamer, RenameAndShorthandNaturalizer, ShorthandNaturalizer};

const LOWERING_FILE_PRAGMA: &str =
    "// @ducktape-generated kind=lowerer-helper stage=selected_module_lowering ignore=detectors";
const LOWERING_GENERATOR_HEADER: &str = "// @ducktape-generator selected_module_lowering";

macro_rules! time_phase {
    ($timings:expr, $name:expr, $body:block) => {{
        let phase_started = std::time::Instant::now();
        let value = $body;
        $timings.add($name, phase_started.elapsed());
        value
    }};
}
pub(crate) use time_phase;

#[derive(Debug, Default, Clone)]
struct PhaseTimings {
    durations: BTreeMap<String, Duration>,
}

impl PhaseTimings {
    fn add(&mut self, name: impl Into<String>, duration: Duration) {
        *self.durations.entry(name.into()).or_default() += duration;
    }

    fn extend_prefixed(&mut self, prefix: &str, other: PhaseTimings) {
        for (name, duration) in other.durations {
            self.add(format!("{prefix}.{name}"), duration);
        }
    }

    fn into_durations(mut self, total: Duration) -> BTreeMap<String, Duration> {
        self.durations.insert("total".to_string(), total);
        self.durations
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct LogicalModuleManifest {
    pub chunks: Vec<LogicalChunkReport>,
    pub counts: LogicalModuleCounts,
    pub duration: Duration,
    pub timings: BTreeMap<String, Duration>,
    pub report_out_dir: Option<String>,
}

pub struct MaterializeLogicalModulesResult {
    pub artifact: ChunkBundle,
    pub manifest: LogicalModuleManifest,
    pub selected_lowerings: Vec<SelectedModuleLowering>,
    pub module_count: usize,
    pub decomposition_by_chunk: HashMap<ChunkId, ChunkDecompositionOutput>,
}

#[derive(Debug, Clone, Serialize)]
pub struct LogicalModuleCounts {
    pub applied: usize,
    pub final_modules: usize,
    pub explicit_logical_modules: usize,
    pub residual_logical_modules: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct LogicalChunkReport {
    pub chunk_id: String,
    pub counts: LogicalChunkCounts,
    pub final_module_contents: Vec<FinalModuleContent>,
    pub requested_logical_modules: Vec<RequestedLogicalModule>,
    /// `purity: pure` hints the analyzer inferred automatically.
    /// Same content as the stderr warnings the build prints; carried
    /// on the report so JSON consumers can pin behavior across runs.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub redundant_purity_hints: Vec<RedundantPurityHint>,
    pub timings: BTreeMap<String, Duration>,
}

#[derive(Debug, Clone, Serialize)]
pub struct LogicalChunkCounts {
    pub applied: usize,
    pub explicit_logical_modules: usize,
    pub final_modules: usize,
    pub residual_logical_modules: usize,
    pub selected_owners: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct FinalModuleContent {
    pub binding_names: Vec<String>,
    pub file: String,
    pub id: String,
    pub member_names: Vec<String>,
    pub path: String,
    pub owner_ids: Vec<String>,
    pub residual: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct RequestedLogicalModule {
    pub id: String,
    pub target_path: String,
    pub residual: bool,
}

#[derive(Debug, Clone)]
pub struct MaterializeLogicalModulesOptions {
    pub chunk_ids: Vec<String>,
    pub file: Option<String>,
    pub prune_other_chunks: bool,
    pub force: bool,
    pub report_out_dir: Option<PathBuf>,
    pub report_summary_path: Option<PathBuf>,
    pub target_dir: String,
}

pub fn materialize_logical_modules(
    mut artifact: ChunkBundle,
    logical_modules: &BTreeMap<String, BTreeMap<String, LogicalModule>>,
    chunk_renames: &BTreeMap<String, ChunkRenames>,
    unassigned_mode: &BTreeMap<String, UnassignedMode>,
    options: MaterializeLogicalModulesOptions,
) -> Result<MaterializeLogicalModulesResult> {
    if options.chunk_ids.is_empty() {
        bail!("materialize_logical_modules requires at least one chunk_id");
    }
    let started = Instant::now();
    let target_dir = normalize_optional_relative_dir(&options.target_dir)?;
    let mut selected_chunk_ids = Vec::new();
    let mut seen = BTreeSet::new();
    for chunk_id in &options.chunk_ids {
        let normalized = normalize_module_path(chunk_id)?;
        if seen.insert(normalized.clone()) {
            selected_chunk_ids.push(normalized);
        }
    }

    let mut report_out_dir = None;
    if let Some(dir) = &options.report_out_dir {
        prepare_output_dir(dir, options.force)?;
        report_out_dir = Some(dir.clone());
    }

    if options.prune_other_chunks {
        prune_artifact_to_chunk_ids(&mut artifact, &selected_chunk_ids);
    }
    let index_started = Instant::now();
    let artifact_indexes = ArtifactIndexes::build(&artifact)?;
    let index_duration = index_started.elapsed();

    let artifact_ref: &ChunkBundle = &artifact;
    // SWC's `swc_common::GLOBALS` is a `scoped_tls` thread-local, so the
    // outer `GLOBALS.set` wrap in `main.rs` / `run_agent` does NOT carry
    // into rayon worker threads. Capture a reference to the current
    // `Globals` and re-set inside each worker closure so `Mark::new()`
    // and `Id`-comparisons stay consistent across the whole pipeline.
    let chunk_results = GLOBALS.with(|globals| {
        selected_chunk_ids
            .par_iter()
            .map(|chunk_id| {
                GLOBALS.set(globals, || {
                    materialize_logical_chunk(MaterializeLogicalChunkInputs {
                        artifact: artifact_ref,
                        artifact_indexes: &artifact_indexes,
                        logical_modules,
                        chunk_renames,
                        unassigned_mode,
                        file: options.file.as_deref(),
                        target_dir: &target_dir,
                        report_out_dir: report_out_dir.as_deref(),
                        chunk_id,
                    })
                })
            })
            .collect::<Result<Vec<_>>>()
    })?;

    let mut reports = Vec::with_capacity(chunk_results.len());
    let mut applied = Vec::<SelectedModuleLowering>::new();
    for chunk_result in &chunk_results {
        if let Some(report_out_dir) = &report_out_dir {
            write_chunk_report_json(
                report_out_dir,
                artifact.chunk_table.name(chunk_result.chunk_id),
                "logical_modules.json",
                &chunk_result.report,
            )?;
        }
        applied.extend(chunk_result.applied.iter().cloned());
        reports.push(chunk_result.report.clone());
    }
    let apply_result = apply_materialized_logical_chunks(artifact, &target_dir, chunk_results)?;
    artifact = apply_result.artifact;
    let decomposition_by_chunk = apply_result.decomposition_by_chunk;

    let module_count: usize = reports.iter().map(|r| r.counts.final_modules).sum();
    let duration = started.elapsed();
    let mut aggregate_timings = aggregate_logical_timings(&reports);
    aggregate_timings.insert("build_artifact_indexes".to_string(), index_duration);
    aggregate_timings.insert("total".to_string(), duration);
    let manifest = LogicalModuleManifest {
        counts: LogicalModuleCounts {
            applied: applied.len(),
            final_modules: reports
                .iter()
                .map(|report| report.counts.final_modules)
                .sum(),
            explicit_logical_modules: reports
                .iter()
                .map(|report| report.counts.explicit_logical_modules)
                .sum(),
            residual_logical_modules: reports
                .iter()
                .map(|report| report.counts.residual_logical_modules)
                .sum(),
        },
        chunks: reports,
        duration,
        timings: aggregate_timings,
        report_out_dir: report_out_dir.as_ref().map(|path| {
            options.report_summary_path.as_ref().map_or_else(
                || module_path_from_path(path),
                |s| manifest_relative_path(s, path),
            )
        }),
    };

    if let Some(summary_path) = options.report_summary_path {
        if let Some(parent) = summary_path.parent() {
            fs::create_dir_all(parent)?;
        }
        serde_json::to_writer_pretty(&fs::File::create(summary_path)?, &manifest)?;
    }
    Ok(MaterializeLogicalModulesResult {
        artifact,
        manifest,
        selected_lowerings: applied,
        module_count,
        decomposition_by_chunk,
    })
}
