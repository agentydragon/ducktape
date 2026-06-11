use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use anyhow::{Context, Result, bail};
use rayon::prelude::*;
use serde::Serialize;
use swc_common::{DUMMY_SP, GLOBALS, SyntaxContext};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitMut, VisitMutWith, VisitWith};

use analysis::{
    AnalysisHints, AtomicUnitConflict, BindingKind, ChunkFactorization, DepKind,
    DynamicImportTarget, KnownEffect, LogicalModule as FactorizationLogicalModule,
    LogicalModuleIndex, ModuleId, OwnerGraphAndUnits, OwnerGraphOptions, OwnerId, RebindFold,
    RedundantPurityHint, StageOneAnalysis, compute_rebind_folds, compute_stage_one_analysis,
    render_atomic_unit_conflict_summary, render_cycle_summary, top_level_id,
};
use artifact::{
    ArtifactIndexes, ArtifactSourceImportResolver, ChunkAnalysisReport, ChunkArtifact, ChunkBundle,
    ChunkDecompositionOutput, ChunkFileRecord, ChunkId, ChunkLogicalModulesSummary, ChunkMetadata,
    ChunkTable, ChunkValidationSummary, DirectoryDependencyFact, FileMetadata, FileRole, JsChunk,
    JsFile, JsFileBody, SelectedModuleLowering, get_chunk_entry_path, join_module_path,
    module_path_dirname, normalize_module_path, normalize_relative_module_specifier,
    relative_module_path,
};
use js_ast::{ParsedJsModule, format_comment_block_lines, set_str_value, str_value};
use output_layout::MODULES_REPORT;
use spec::{
    BindingSourceKind, ChunkRenames, LogicalModule, MemberEffect, MemberPurity, UnassignedMode,
};

mod anonymous;
mod body_facts;
mod chunk_ast;
mod chunk_renames;
mod exports;
mod import_emit;
mod imports_cross;
mod imports_runtime;
mod io;
mod lower;
mod materialize;
mod naturalize;
mod ordinal;
mod plan_references;
mod plans;
mod rewrite_runtime;
mod runtime_imports;
mod scope_names;
mod util;
mod visitors;

use anonymous::resolve_anonymous_statement_ordinals;
use body_facts::{ModuleBodyFacts, collect_module_body_facts};
use chunk_ast::{
    ChunkAstAnalysis, TopLevelDecl, analyze_chunk_ast, binding_ids, binding_names, declaration_ids,
    declaration_names, top_level_declaration_ids, top_level_declaration_names,
};
use chunk_renames::collect_chunk_renames;
use exports::{
    auto_grown_residual_exports, entry_exports_for_moved_bindings, export_named_for_bindings,
    reject_duplicate_export_names, reject_duplicate_member_bindings, trim_dead_named_specifiers,
};
use imports_cross::{
    collect_entry_exports_by_original_local, cross_module_imports_for_plan, final_module_exports,
    phantom_side_effect_imports, residual_entry_imports_for_moved_body,
};
use imports_runtime::{
    group_specifiers_into_import_decls, import_decl_module_item, resolve_imported_binding,
    source_chunk_imports_for_moved_body,
};
use io::{prepare_output_dir, prune_artifact_to_chunk_ids, write_chunk_report_json};
use lower::{
    LowerChunkAst, LowerChunkContext, LowerChunkInputs, LowerChunkPlan, LowerChunkSpecFacts,
    LoweredChunk, lower_chunk,
};
use materialize::{
    ChunkContext, ChunkSpec, MaterializeLogicalChunkInputs, apply_materialized_logical_chunks,
    materialize_logical_chunk,
};
use naturalize::{NaturalizedRenames, naturalize_module_body};
use plan_references::{
    ArtifactSourceImportResolutionCache, EntryExport, ModuleReferenceNeeds, RuntimeImportLookup,
    collect_imported_reexports_by_module, plan_module_reference_needs,
};
use plans::{LogicalRequest, MemberRequest, ModulePlan, logical_requests_for_chunk};
use rewrite_runtime::rewrite_runtime_sources_for_target;
use runtime_imports::{
    RuntimeImportFacts, RuntimeImportInfo, RuntimeImportKind, imported_binding_named_specifier,
    record_runtime_imports, runtime_reimport_specifier,
};
use util::normalize_optional_relative_dir;
use visitors::{IdentifierRenamer, RenameAndShorthandNaturalizer, ShorthandNaturalizer};

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

pub struct MaterializeLogicalModulesResult {
    pub artifact: ChunkBundle,
    pub selected_lowerings: Vec<SelectedModuleLowering>,
    pub module_count: usize,
    pub decomposition_by_chunk: HashMap<ChunkId, ChunkDecompositionOutput>,
    /// Spec claims that named a binding for which no top-level
    /// declaration exists in the source chunk. Previously dropped
    /// silently — the binding would fall through to the residual and
    /// the named module's export list would be one entry short. Now
    /// collected across every chunk so the pipeline keeps emitting
    /// (the chunk lowers as if the spec had not claimed the missing
    /// name), then fails at the end with the full list. See
    /// [`UnmatchedSpecClaim`].
    pub unmatched_spec_claims: Vec<UnmatchedSpecClaim>,
}

/// A `define_logical_module` member whose `binding.name` did not
/// resolve to a top-level declaration in the chunk. Surfaced after
/// the pipeline finishes so the build prints every offender at once
/// instead of failing on the first chunk.
#[derive(Debug, Clone, Serialize)]
pub struct UnmatchedSpecClaim {
    pub chunk_id: String,
    /// Claiming module, by canonical [`spec::ModulePath`].
    pub module_path: spec::ModulePath,
    pub binding_name: String,
    pub export_name: String,
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
    pub timings: BTreeMap<String, Duration>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChunkModulesCounts {
    pub applied: usize,
    pub selected_owners: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct FinalModuleContent {
    pub binding_names: Vec<String>,
    pub file: String,
    pub member_names: Vec<String>,
    /// Canonical [`spec::ModulePath`] (the report is per-chunk, so
    /// the chunk is implicit).
    pub path: spec::ModulePath,
    pub owner_ids: Vec<String>,
    pub residual: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct RequestedLogicalModule {
    /// Canonical [`spec::ModulePath`] of the requested target.
    pub target_path: spec::ModulePath,
    pub residual: bool,
}

#[derive(Debug, Clone)]
pub struct MaterializeLogicalModulesOptions {
    pub chunk_ids: Vec<String>,
    pub file: Option<String>,
    pub prune_other_chunks: bool,
    pub report_out_dir: Option<PathBuf>,
    pub target_dir: String,
}

pub fn materialize_logical_modules(
    mut artifact: ChunkBundle,
    logical_modules: &BTreeMap<String, BTreeMap<String, LogicalModule>>,
    chunk_renames: &BTreeMap<String, ChunkRenames>,
    unassigned_mode: &BTreeMap<String, UnassignedMode>,
    chunk_analysis_options: &BTreeMap<String, OwnerGraphOptions>,
    options: MaterializeLogicalModulesOptions,
) -> Result<MaterializeLogicalModulesResult> {
    if options.chunk_ids.is_empty() {
        bail!("materialize_logical_modules requires at least one chunk_id");
    }
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
        prepare_output_dir(dir)?;
        report_out_dir = Some(dir.clone());
    }

    if options.prune_other_chunks {
        prune_artifact_to_chunk_ids(&mut artifact, &selected_chunk_ids);
    }
    let artifact_indexes = ArtifactIndexes::build(&artifact)?;

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
                        context: ChunkContext {
                            artifact: artifact_ref,
                            artifact_indexes: &artifact_indexes,
                            chunk_id,
                            file: options.file.as_deref(),
                            target_dir: &target_dir,
                            report_out_dir: report_out_dir.as_deref(),
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
            .collect::<Result<Vec<_>>>()
    })?;

    let mut reports = Vec::with_capacity(chunk_results.len());
    let mut applied = Vec::<SelectedModuleLowering>::new();
    let mut unmatched_spec_claims = Vec::<UnmatchedSpecClaim>::new();
    for chunk_result in &chunk_results {
        if let Some(report_out_dir) = &report_out_dir {
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
    }
    let apply_result = apply_materialized_logical_chunks(artifact, &target_dir, chunk_results)?;
    artifact = apply_result.artifact;
    let decomposition_by_chunk = apply_result.decomposition_by_chunk;

    let module_count: usize = reports.iter().map(|r| r.final_module_contents.len()).sum();
    Ok(MaterializeLogicalModulesResult {
        artifact,
        selected_lowerings: applied,
        module_count,
        decomposition_by_chunk,
        unmatched_spec_claims,
    })
}
