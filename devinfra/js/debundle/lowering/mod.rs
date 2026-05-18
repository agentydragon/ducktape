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
    AnalysisHints, AtomicUnitConflict, BindingKind, BindingName, DepKind, KnownEffect,
    LogicalModule as ScheduleLogicalModule, LogicalModuleIndex, ModuleId, OwnerGraphAndUnits,
    OwnerId, RedundantPureMemberReason, RedundantPurityHint, RedundantPurityReason, Schedule,
    analyze_chunk, compute_owner_graph_and_units, render_atomic_unit_conflict_summary,
    render_cycle_summary, top_level_id,
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
    ChunkAstAnalysis, TopLevelDecl, analyze_chunk_ast, binding_names, declaration_names,
    top_level_declaration_ids,
};
use chunk_renames::collect_chunk_renames;
use exports::{
    auto_grown_residual_exports, entry_exports_for_moved_bindings, export_named_for_bindings,
    reject_duplicate_export_names, reject_duplicate_member_bindings, trim_dead_named_specifiers,
};
use imports_cross::{
    collect_entry_exports_by_original_local, cross_module_imports_for_plan, final_module_exports,
    module_export_ident_name, residual_entry_imports_for_moved_body,
};
use imports_runtime::{
    import_decl_module_item, resolve_imported_binding, source_chunk_imports_for_moved_body,
};
use lower::{LowerChunkInputs, LoweredChunk, lower_chunk};
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
#[allow(unused_imports)]
use util::{
    body_index_for_statement_ordinal, collect_local_binding_names, collect_occupied_local_names,
    disambiguate_import_locals, disambiguate_residual_entry_import_locals, import_decl_for_plan,
    is_identifier_like, is_valid_js_identifier, normalize_optional_relative_dir,
    prepare_output_dir, preserve_export_specifier_names, prune_artifact_to_chunk_ids,
    relative_source, remaining_item_after_selection, render_atomic_unit_cause_guidance,
    statement_ordinal_for_body_index, target_file_for_request, write_chunk_report_json,
};
use visitors::{IdentifierRenamer, RenameAndShorthandNaturalizer, ShorthandNaturalizer};

const LOWERING_FILE_PRAGMA: &str =
    "// @ducktape-generated kind=lowerer-helper stage=selected_module_lowering ignore=detectors";
const LOWERING_GENERATOR_HEADER: &str = "// @ducktape-generator selected_module_lowering";

#[macro_export]
macro_rules! time_phase {
    ($timings:expr, $name:expr, $body:block) => {{
        let phase_started = std::time::Instant::now();
        let value = $body;
        $timings.add($name, phase_started.elapsed());
        value
    }};
}

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

fn aggregate_logical_timings(reports: &[LogicalChunkReport]) -> BTreeMap<String, Duration> {
    let mut timings = BTreeMap::<String, Duration>::new();
    for report in reports {
        for (name, duration) in &report.timings {
            *timings.entry(format!("chunks.{name}")).or_default() += *duration;
        }
    }
    timings
}

struct MaterializeLogicalChunkInputs<'a> {
    artifact: &'a ChunkBundle,
    artifact_indexes: &'a ArtifactIndexes,
    logical_modules: &'a BTreeMap<String, BTreeMap<String, LogicalModule>>,
    chunk_renames: &'a BTreeMap<String, ChunkRenames>,
    unassigned_mode: &'a BTreeMap<String, UnassignedMode>,
    file: Option<&'a str>,
    target_dir: &'a str,
    report_out_dir: Option<&'a Path>,
    chunk_id: &'a str,
}

struct MaterializedLogicalChunk {
    chunk_id: ChunkId,
    target_file: String,
    source_path: String,
    files: Vec<JsFile>,
    file_records: Vec<(String, FileRole)>,
    applied: Vec<SelectedModuleLowering>,
    report: LogicalChunkReport,
}

fn materialize_logical_chunk(
    inputs: MaterializeLogicalChunkInputs<'_>,
) -> Result<MaterializedLogicalChunk> {
    let MaterializeLogicalChunkInputs {
        artifact,
        artifact_indexes,
        logical_modules,
        chunk_renames,
        unassigned_mode,
        file,
        target_dir,
        report_out_dir,
        chunk_id,
    } = inputs;
    // The spec validator (`validate_transform_spec`) enforces that
    // every materialised chunk has an `unassigned_mode` entry, so
    // this lookup must not miss. Missing here is a bug in the
    // validator, not a recoverable spec error.
    let chunk_unassigned_mode = unassigned_mode.get(chunk_id).cloned().with_context(|| {
        format!("materialize_logical_modules missing unassigned_mode for chunk: {chunk_id}")
    })?;
    let chunk_id_interned = artifact
        .chunk_table
        .get(chunk_id)
        .with_context(|| format!("materialize_logical_modules unknown chunk: {chunk_id}"))?;
    let chunk_started = Instant::now();
    let mut timings = PhaseTimings::default();
    let target_file = time_phase!(timings, "resolve_entry", {
        file.map(normalize_module_path)
            .transpose()?
            .or_else(|| get_chunk_entry_path(artifact, chunk_id_interned))
            .with_context(|| {
                format!(
                    "materialize_logical_modules could not determine entry file for chunk: {chunk_id}"
                )
            })
    })?;
    let runtime_file = artifact
        .js_chunk(chunk_id_interned)?
        .get_file(&target_file)
        .with_context(|| {
            format!("materialize_logical_modules missing entry file for chunk: {chunk_id}")
        })?;
    let runtime_ast = runtime_file.ast().with_context(|| {
        format!("materialize_logical_modules missing entry AST for chunk: {chunk_id}")
    })?;
    // Chunk-wide `top_level_mark` for resolving spec-derived String
    // binding names to hygiene-aware `Id`s via `top_level_id`.
    let chunk_top_level_mark = runtime_ast.top_level_mark;
    let header_lines = runtime_file.header_lines.clone();
    let source_path = runtime_file.metadata.source_path.clone();
    let chunk_ast_analysis = time_phase!(timings, "analyze_chunk_ast", {
        analyze_chunk_ast(&runtime_ast.module)
    });
    let ChunkAstAnalysis {
        runtime_import_facts,
        declarations,
        declaration_by_name,
        destructure_siblings,
        pre_existing_entry_exports,
    } = chunk_ast_analysis;
    let requests = time_phase!(timings, "build_requests", {
        logical_requests_for_chunk(
            logical_modules.get(chunk_id),
            &chunk_unassigned_mode,
            chunk_renames.contains_key(chunk_id),
            chunk_id,
            target_dir,
        )
    })?;
    let mut explicit_requests = requests
        .iter()
        .filter(|request| !request.residual)
        .cloned()
        .collect::<Vec<_>>();
    let residual_request = requests.iter().find(|request| request.residual).cloned();

    let build_module_plans_started = Instant::now();
    let mut binding_assignment = BTreeMap::<String, usize>::new();
    let mut anonymous_ordinal_assignment = BTreeMap::<usize, usize>::new();
    let mut module_plans = Vec::new();
    let mut bindings_catalogue = HashMap::<Id, BindingKind>::new();
    let mut imported_binding_resolver =
        ArtifactSourceImportResolutionCache::new(artifact, artifact_indexes);
    let mut imported_from_by_src = BTreeMap::<String, String>::new();
    for (index, request) in explicit_requests.iter_mut().enumerate() {
        let mut bindings = HashMap::<String, String>::new();
        let anonymous_statement_ordinals =
            resolve_anonymous_statement_ordinals(request, &runtime_ast.module)?;
        for ordinal in &anonymous_statement_ordinals {
            if let Some(existing) = anonymous_ordinal_assignment.get(ordinal).copied() {
                let existing_id: String = module_plans
                    .get(existing)
                    .map(|plan: &ModulePlan| plan.id.clone())
                    .unwrap_or_else(|| format!("<plan#{existing}>"));
                bail!(
                    "anonymous_statements[].match in module {} also matches the \
                     top-level statement at ordinal {} already claimed by module {}; \
                     each anonymous statement may belong to at most one logical \
                     module.",
                    request.id,
                    ordinal,
                    existing_id,
                );
            }
            anonymous_ordinal_assignment.insert(*ordinal, index);
        }
        let dest_target_file = target_file_for_request(target_dir, &request.target_path)?;
        let module_id = ModuleId(LogicalModuleIndex(index));
        for member in &request.members {
            if let Some(existing_kind) = bindings_catalogue
                .iter()
                .find(|(id, _)| id.0.as_ref() == member.binding.as_str())
                .map(|(_, v)| v)
            {
                let existing_id = match existing_kind {
                    BindingKind::Owned {
                        owner: ModuleId(LogicalModuleIndex(owner_index)),
                    } => module_plans
                        .get(*owner_index)
                        .map(|plan| plan.id.clone())
                        .unwrap_or_else(|| format!("<plan#{owner_index}>")),
                    BindingKind::Imported {
                        re_exporter: ModuleId(LogicalModuleIndex(re_index)),
                        ..
                    } => module_plans
                        .get(*re_index)
                        .map(|plan| plan.id.clone())
                        .unwrap_or_else(|| format!("<plan#{re_index}>")),
                };
                bail!(
                    "Duplicate binding claim for {:?} in chunk {chunk_id:?}: already \
                     claimed by module {existing_id} and now also claimed by module \
                     {}. Each binding may belong to exactly one logical module. \
                     Different selector forms (`{{name: foo}}` vs \
                     `{{name: foo, kind: class_declaration}}`) that resolve to the \
                     same source declaration still count as duplicates. To expose a \
                     binding under multiple readable names, list all the renames in \
                     one module.",
                    member.binding,
                    request.id,
                );
            }
            if member.is_import_specifier {
                let (imported_name, imported_from) = resolve_imported_binding(
                    &mut imported_binding_resolver,
                    &runtime_import_facts,
                    chunk_id,
                    &target_file,
                    &member.binding,
                    &mut imported_from_by_src,
                )?;
                bindings_catalogue.insert(
                    top_level_id(member.binding.as_str(), chunk_top_level_mark),
                    BindingKind::Imported {
                        imported_name,
                        imported_from,
                        re_exporter: module_id,
                        public_name: member.export_name.clone(),
                    },
                );
            } else {
                bindings.insert(member.binding.clone(), member.export_name.clone());
            }
        }
        for binding in bindings.keys() {
            if declaration_by_name.contains_key(binding) {
                binding_assignment.insert(binding.clone(), index);
                bindings_catalogue.insert(
                    top_level_id(binding.as_str(), chunk_top_level_mark),
                    BindingKind::Owned { owner: module_id },
                );
            }
        }
        module_plans.push(ModulePlan {
            id: request.id.clone(),
            target_file: dest_target_file,
            target_path: request.target_path.clone(),
            explicit: true,
            bindings,
            anonymous_statement_ordinals,
        });
    }
    drop(imported_binding_resolver);

    // Destructure-atomicity: a destructuring declarator like
    // `const { x, y } = obj` binds multiple names from a single
    // pattern that the lowerer's `split_var_decl` moves as one
    // unit. If the spec claims any one binding from such a
    // pattern, every sibling binding must travel to the same
    // module — otherwise the residual's export list would list a
    // name whose declarator has already moved away, and `node`
    // would reject the resulting module with
    // `SyntaxError: Export 'y' is not defined in module`.
    //
    // Implicitly-pulled siblings join the claimed module with
    // their own binding name as the export name. They aren't
    // separately spec'd, but the destructure pattern must keep
    // its full name set together regardless. Conflicting claims
    // (two siblings claimed by different modules) are rejected.
    for (claimed_name, sibling_set) in &destructure_siblings {
        let Some(&owner_index) = binding_assignment.get(claimed_name) else {
            continue;
        };
        let owner_id = ModuleId(LogicalModuleIndex(owner_index));
        for sibling in sibling_set {
            if sibling == claimed_name {
                continue;
            }
            match binding_assignment.get(sibling).copied() {
                None => {
                    binding_assignment.insert(sibling.clone(), owner_index);
                    bindings_catalogue.insert(
                        top_level_id(sibling.as_str(), chunk_top_level_mark),
                        BindingKind::Owned { owner: owner_id },
                    );
                    let plan = &mut module_plans[owner_index];
                    plan.bindings.insert(sibling.clone(), sibling.clone());
                }
                Some(other_index) if other_index != owner_index => {
                    let owner_plan_id = module_plans[owner_index].id.clone();
                    let other_plan_id = module_plans[other_index].id.clone();
                    bail!(
                        "destructure declarator binds {claimed_name} (claimed by module \
                         {owner_plan_id}) and {sibling} (claimed by module {other_plan_id}); \
                         destructuring declarators must move atomically — claim both \
                         bindings from the same module or claim neither.",
                    );
                }
                Some(_) => {}
            }
        }
    }

    // The catchall destination index, or `None` when the chunk has
    // no residual landing site (default `InlineInEntry` mode with
    // no fallback request, or `MiniFactors` mode). When set, points
    // either to a synthesized memberless residual plan (built below)
    // or to an explicit logical-module plan whose target matches
    // `unassigned_mode: catchall_file { target }` and which is
    // therefore the designated overflow destination.
    let mut residual_plan_index: Option<usize> = None;
    let catchall_target_for_overflow = chunk_unassigned_mode.catchall_file_target();
    if let Some(residual) = &residual_request {
        let residual_index = module_plans.len();
        let residual_module_id = ModuleId(LogicalModuleIndex(residual_index));
        let mut residual_bindings = HashMap::<String, String>::new();
        for decl in &declarations {
            for name in &decl.names {
                if !binding_assignment.contains_key(name) {
                    binding_assignment.insert(name.clone(), residual_index);
                    residual_bindings.insert(name.clone(), name.clone());
                    bindings_catalogue.insert(
                        top_level_id(name.as_str(), chunk_top_level_mark),
                        BindingKind::Owned {
                            owner: residual_module_id,
                        },
                    );
                }
            }
        }
        if !residual_bindings.is_empty() {
            module_plans.push(ModulePlan {
                id: residual.id.clone(),
                target_file: target_file_for_request(target_dir, &residual.target_path)?,
                target_path: residual.target_path.clone(),
                explicit: false,
                bindings: residual_bindings,
                anonymous_statement_ordinals: Vec::new(),
            });
            residual_plan_index = Some(residual_index);
        }
    } else if let Some(catchall_target) = catchall_target_for_overflow {
        // No memberless residual request was synthesized — an
        // explicit `logical_modules` entry already pinned itself at
        // the catchall target. Append unclaimed bindings to that
        // plan so the residual sweep still has a home, and flip
        // its `explicit` flag so downstream consumers see it as
        // the residual destination (residual flag on the schedule
        // module, OutputRole::ResidualModule in artifact metadata,
        // residual_logical_modules count on the chunk report).
        let owner_index = module_plans
            .iter()
            .position(|plan| plan.target_path == catchall_target);
        if let Some(owner_index) = owner_index {
            let owner_id = ModuleId(LogicalModuleIndex(owner_index));
            let owner_plan = &mut module_plans[owner_index];
            owner_plan.explicit = false;
            for decl in &declarations {
                for name in &decl.names {
                    if !binding_assignment.contains_key(name) {
                        binding_assignment.insert(name.clone(), owner_index);
                        owner_plan
                            .bindings
                            .entry(name.clone())
                            .or_insert_with(|| name.clone());
                        bindings_catalogue.insert(
                            top_level_id(name.as_str(), chunk_top_level_mark),
                            BindingKind::Owned { owner: owner_id },
                        );
                    }
                }
            }
            residual_plan_index = Some(owner_index);
        }
    }
    timings.add("build_module_plans", build_module_plans_started.elapsed());

    let chunk_renames_map = time_phase!(timings, "collect_chunk_renames", {
        chunk_renames
            .get(chunk_id)
            .map(collect_chunk_renames)
            .transpose()
    })?
    .unwrap_or_default();

    let (schedule, redundant_purity_hints) = {
        // Spec annotations carried on any member form (logical-module
        // member, chunk_renames member) propagate the same way:
        // collect them by local binding name and feed them into fact
        // analysis. They are semantic trust assertions, not ownership
        // claims; binding patches routed through chunk_renames still
        // do not force factorizer grouping.
        let analysis_hints: AnalysisHints = time_phase!(timings, "collect_analysis_hints", {
            let mut hints = AnalysisHints::default();
            for req in &explicit_requests {
                for m in &req.members {
                    if m.purity == MemberPurity::Pure {
                        hints.declared_pure.insert(m.binding.clone());
                    }
                    if m.purity == MemberPurity::PureNew {
                        hints.declared_pure_new.insert(m.binding.clone());
                    }
                    if !m.pure_members.is_empty() {
                        hints
                            .declared_pure_members
                            .entry(m.binding.clone())
                            .or_default()
                            .extend(m.pure_members.iter().cloned());
                    }
                    if let Some(effect) = known_effect_from_member_effect(m.effect) {
                        hints.known_effects.insert(m.binding.clone(), effect);
                    }
                }
            }
            if let Some(cr) = chunk_renames.get(chunk_id) {
                for m in &cr.members {
                    if m.purity == MemberPurity::Pure {
                        hints.declared_pure.insert(m.selector.binding.name.clone());
                    }
                    if m.purity == MemberPurity::PureNew {
                        hints
                            .declared_pure_new
                            .insert(m.selector.binding.name.clone());
                    }
                    if !m.pure_members.is_empty() {
                        hints
                            .declared_pure_members
                            .entry(m.selector.binding.name.clone())
                            .or_default()
                            .extend(m.pure_members.iter().cloned());
                    }
                    if let Some(effect) = known_effect_from_member_effect(m.effect) {
                        hints
                            .known_effects
                            .insert(m.selector.binding.name.clone(), effect);
                    }
                }
            }
            hints
        });
        let line_index = time_phase!(timings, "build_source_line_index", {
            runtime_ast.line_index()
        });
        let analysis = time_phase!(timings, "analyze_chunk", {
            analyze_chunk(
                &runtime_ast.module,
                &analysis_hints,
                Some(&source_path),
                |span| line_index.line_range_for_span(span),
            )
        });
        // Per-hint warnings on stderr: each `purity: pure` spec hint
        // the analyzer infers automatically (binding's body classifies
        // Pure without the override, or admits as PlainData). Surfaced
        // every build so spec authors are nudged to prune load-free
        // hints — every such hint is an extra trust assertion the
        // validator can't re-verify, and the shrinking trust surface
        // is the point of recursive purity inference.
        for hint in &analysis.redundant_purity_hints {
            eprintln!(
                "warning: chunk {chunk_id}: `purity: pure` hint on binding `{binding}` is redundant — \
                 the analyzer infers {reason} for this binding without the hint and the override is a no-op. \
                 Remove the hint from the spec.",
                binding = hint.binding_name,
                reason = match hint.reason {
                    RedundantPurityReason::InferredPureFunction =>
                        "pure (the function body classifies Pure by recursive analysis)",
                    RedundantPurityReason::InferredPlainDataBinding =>
                        "PlainData (chunk-local const/let plain literal with no chunk-wide writes through the binding)",
                },
            );
        }
        for hint in &analysis.redundant_pure_member_hints {
            eprintln!(
                "warning: chunk {chunk_id}: `pure_members: [{property}]` on binding `{binding}` \
                 is redundant — the analyzer infers {reason} without the hint. \
                 Remove the entry from the spec.",
                binding = hint.binding_name,
                property = hint.property,
                reason = match hint.reason {
                    RedundantPureMemberReason::WhitelistedStaticCall =>
                        "pure via PURE_STATIC_CALLS (already on the global-receiver whitelist)",
                },
            );
        }
        if let Some(ord) = analysis.top_level_await {
            anyhow::bail!(
                "materialize_logical_modules: chunk {chunk_id} has top-level `await` \
                 at statement #{ordinal} (TLA); the debundler's realizability theorem \
                 does not cover async modules (DESIGN.md A2). Wrap the awaited code \
                 in an async function or rewrite as a synchronous initialization.",
                ordinal = ord.0,
            );
        }
        let precomputed = time_phase!(timings, "compute_owner_graph_and_units", {
            compute_owner_graph_and_units(&analysis.facts)
        });
        if matches!(chunk_unassigned_mode, UnassignedMode::MiniFactors) {
            time_phase!(timings, "synthesize_mini_factor_plans", {
                synthesize_mini_factor_plans(
                    &precomputed,
                    &runtime_ast.module.body,
                    residual_plan_index,
                    &mut module_plans,
                    &mut binding_assignment,
                    &mut bindings_catalogue,
                    &mut anonymous_ordinal_assignment,
                    chunk_top_level_mark,
                    target_dir,
                )
            })?;
        }
        let chunk_top_level_mark = runtime_ast.top_level_mark;
        let mut logical_modules: Vec<ScheduleLogicalModule> =
            time_phase!(timings, "project_schedule_modules", {
                module_plans
                    .iter()
                    .map(|plan| ScheduleLogicalModule {
                        id: plan.id.clone(),
                        target_file: plan.target_file.clone(),
                        residual: !plan.explicit,
                        rename_map: plan
                            .bindings
                            .iter()
                            .map(|(local, exported)| {
                                (
                                    top_level_id(local.as_str(), chunk_top_level_mark),
                                    exported.as_str().into(),
                                )
                            })
                            .collect(),
                        // Schedule's owner graph uses post-comma-list-split
                        // `StatementOrdinal`s; convert body indices here so
                        // the destination override targets the right owner
                        // node (an anon body item is always a single
                        // post-split position, but earlier comma-list
                        // var-decls in the chunk shift the count).
                        anonymous_statement_ordinals: plan
                            .anonymous_statement_ordinals
                            .iter()
                            .map(|body_idx| {
                                statement_ordinal_for_body_index(
                                    &runtime_ast.module.body,
                                    *body_idx,
                                )
                            })
                            .collect(),
                    })
                    .collect()
            });
        // Commit 1 transitional behavior: the partition's "default
        // destination" — the module owners with no claim fall back to —
        // is a schedule-only sentinel logical module appended past
        // `module_plans.len()`. The emit loop iterates `module_plans`,
        // so the sentinel never gets emitted as a file. Anonymous
        // statements without an explicit logical-module
        // `anonymous_statements` match thus stay in the sentinel,
        // preserving the pre-refactor split where anon-fallback was a
        // distinct destination from the residual logical module (which
        // only held named-unclaimed bindings). Commit 2 collapses this
        // sentinel back into the residual module via explicit
        // `anonymous_statement_ordinals` routing.
        let sentinel_residual_target = chunk_unassigned_mode
            .catchall_file_target()
            .map(|t| target_file_for_request(target_dir, t))
            .transpose()?
            .unwrap_or_else(|| target_file.clone());
        let sentinel_idx = logical_modules.len();
        logical_modules.push(ScheduleLogicalModule {
            id: format!("{chunk_id}::anon_residual_sentinel"),
            target_file: sentinel_residual_target,
            residual: true,
            rename_map: HashMap::new(),
            anonymous_statement_ordinals: Vec::new(),
        });
        let default_destination = ModuleId(LogicalModuleIndex(sentinel_idx));
        let redundant_purity_hints = analysis.redundant_purity_hints;
        let schedule_chunk_renames: HashMap<Id, swc_atoms::Atom> = chunk_renames_map
            .iter()
            .map(|(local, exported)| {
                (
                    top_level_id(local.as_str(), chunk_top_level_mark),
                    exported.as_str().into(),
                )
            })
            .collect();
        let schedule = time_phase!(timings, "build_schedule", {
            Schedule::build_with(
                chunk_id.to_string(),
                analysis.facts,
                precomputed,
                bindings_catalogue,
                logical_modules,
                schedule_chunk_renames,
                default_destination,
            )
        });
        (schedule, redundant_purity_hints)
    };
    let schedule_report = time_phase!(timings, "validate_schedule", { schedule.validate() });
    if let Some(report_out_dir) = report_out_dir {
        time_phase!(timings, "write_schedule_report", {
            write_chunk_report_json(report_out_dir, chunk_id, "schedule.json", &schedule_report)
        })?;
        let owner_graph_report = time_phase!(timings, "build_owner_graph_report", {
            schedule.owner_graph_report()
        });
        time_phase!(timings, "write_owner_graph_report", {
            write_chunk_report_json(
                report_out_dir,
                chunk_id,
                "owner_graph.json",
                &owner_graph_report,
            )
        })?;
    }

    if !schedule_report.atomic_unit_conflicts.is_empty() {
        let summary =
            render_atomic_unit_conflict_summary(&schedule_report.atomic_unit_conflicts, &|id| {
                schedule.module_name(id)
            });
        let causes = render_atomic_unit_cause_guidance(&schedule_report.atomic_unit_conflicts);
        bail!(
            "materialize_logical_modules: chunk {chunk_id} has {n} atomic-factor-unit conflict(s) — the spec assigns members of one atomic factor unit to different destination modules, forming a cycle in the module dep graph that the constraining-edge SCC analysis says is unrealizable. Atomic factor units come from FACTORIZE.md's `G_atomic` SCC over the owner graph; every member must co-locate. {causes}Resolve by reconciling each unit's claims into a single destination. Full evidence written to <reports>/{chunk_id}/schedule.json; owner graph written to <reports>/{chunk_id}/owner_graph.json. Summary:\n{summary}",
            n = schedule_report.atomic_unit_conflicts.len(),
        );
    }

    if !schedule_report.cycles.is_empty() {
        if let Some(report_out_dir) = report_out_dir {
            time_phase!(timings, "write_cycles_report", {
                write_chunk_report_json(
                    report_out_dir,
                    chunk_id,
                    "cycles.json",
                    &schedule_report.cycles,
                )
            })?;
        }
        let summary = render_cycle_summary(&schedule_report.cycles);
        bail!(
            "materialize_logical_modules: chunk {chunk_id} has {} cycle(s) in the imports + side-effect module dep graph; spec is unrealizable. Resolve by colocating cyclically-coupled bindings or moving the constraining owner endpoints. Full cycle evidence written to <reports>/{chunk_id}/cycles.json; owner graph written to <reports>/{chunk_id}/owner_graph.json. Summary:\n{summary}",
            schedule_report.cycles.len(),
        );
    }

    let lowered = time_phase!(timings, "lower_chunk_total", {
        lower_chunk(LowerChunkInputs {
            artifact,
            artifact_indexes,
            runtime_ast,
            header_lines: &header_lines,
            entry_file: &target_file,
            chunk_id,
            source_path: &source_path,
            declarations: &declarations,
            declaration_by_name: &declaration_by_name,
            module_plans: &module_plans,
            binding_assignment: &binding_assignment,
            anonymous_ordinal_assignment: &anonymous_ordinal_assignment,
            schedule: &schedule,
            chunk_renames: &chunk_renames_map,
            runtime_import_facts: &runtime_import_facts,
            pre_existing_entry_exports: &pre_existing_entry_exports,
        })
    })?;
    let LoweredChunk {
        files,
        file_records,
        applied,
        timings: lower_timings,
    } = lowered;
    timings.extend_prefixed("lower", lower_timings);

    let final_modules = time_phase!(timings, "build_final_module_report", {
        module_plans
            .iter()
            .map(|plan| {
                let mut sorted: Vec<(&String, &String)> = plan.bindings.iter().collect();
                sorted.sort_by(|a, b| a.0.cmp(b.0));
                let binding_names: Vec<String> = sorted.iter().map(|(k, _)| (*k).clone()).collect();
                let member_names: Vec<String> = sorted.iter().map(|(_, v)| (*v).clone()).collect();
                let owner_ids = schedule
                    .owner_report_ids_for_bindings(binding_names.iter().map(String::as_str));
                FinalModuleContent {
                    binding_names,
                    file: plan.target_file.clone(),
                    id: plan.id.clone(),
                    member_names,
                    path: plan.target_path.clone(),
                    owner_ids,
                    residual: !plan.explicit,
                }
            })
            .collect::<Vec<_>>()
    });
    let timings = timings.into_durations(chunk_started.elapsed());
    let report = LogicalChunkReport {
        chunk_id: chunk_id.to_string(),
        counts: LogicalChunkCounts {
            applied: applied.len(),
            explicit_logical_modules: module_plans.iter().filter(|plan| plan.explicit).count(),
            final_modules: module_plans.len(),
            residual_logical_modules: module_plans.iter().filter(|plan| !plan.explicit).count(),
            selected_owners: binding_assignment.len(),
        },
        final_module_contents: final_modules,
        requested_logical_modules: requests
            .iter()
            .map(|request| RequestedLogicalModule {
                id: request.id.clone(),
                target_path: request.target_path.clone(),
                residual: request.residual,
            })
            .collect(),
        redundant_purity_hints,
        timings,
    };
    Ok(MaterializedLogicalChunk {
        chunk_id: chunk_id_interned,
        target_file,
        source_path,
        files,
        file_records,
        applied,
        report,
    })
}

struct ApplyChunksResult {
    artifact: ChunkBundle,
    decomposition_by_chunk: HashMap<ChunkId, ChunkDecompositionOutput>,
}

fn apply_materialized_logical_chunks(
    artifact: ChunkBundle,
    target_dir: &str,
    chunks: Vec<MaterializedLogicalChunk>,
) -> Result<ApplyChunksResult> {
    let chunk_table = artifact.chunk_table.clone();
    let mut replacements = BTreeMap::<ChunkId, MaterializedLogicalChunk>::new();
    for chunk in chunks {
        let chunk_id = chunk.chunk_id;
        if replacements.insert(chunk_id, chunk).is_some() {
            bail!(
                "materialize_logical_modules produced duplicate chunk_id: {}",
                chunk_table.name(chunk_id)
            );
        }
    }

    let source_chunks = artifact.chunks;
    let mut output_chunks = Vec::with_capacity(source_chunks.len() + replacements.len());
    let mut decomposition_by_chunk = HashMap::new();
    for chunk_artifact in source_chunks {
        if let Some(replacement) = replacements.remove(&chunk_artifact.chunk_id) {
            let (new_artifact, decomposition) = materialized_chunk_artifact(
                target_dir,
                &chunk_table,
                Some(chunk_artifact.analysis),
                replacement,
            );
            decomposition_by_chunk.insert(new_artifact.chunk_id, decomposition);
            output_chunks.push(new_artifact);
        } else {
            output_chunks.push(chunk_artifact);
        }
    }
    for replacement in replacements.into_values() {
        let (new_artifact, decomposition) =
            materialized_chunk_artifact(target_dir, &chunk_table, None, replacement);
        decomposition_by_chunk.insert(new_artifact.chunk_id, decomposition);
        output_chunks.push(new_artifact);
    }
    Ok(ApplyChunksResult {
        artifact: ChunkBundle {
            chunks: output_chunks,
            chunk_table: artifact.chunk_table,
        },
        decomposition_by_chunk,
    })
}

fn materialized_chunk_artifact(
    target_dir: &str,
    chunk_table: &ChunkTable,
    base_analysis: Option<ChunkAnalysis>,
    chunk: MaterializedLogicalChunk,
) -> (ChunkArtifact, ChunkDecompositionOutput) {
    let MaterializedLogicalChunk {
        chunk_id,
        target_file,
        source_path,
        files,
        file_records,
        applied,
        report,
    } = chunk;
    let chunk_name = chunk_table.name(chunk_id).to_string();
    let manifest_files = file_records
        .iter()
        .map(|(file, role)| ChunkFileRecord {
            file: file.clone(),
            role: *role,
        })
        .collect();
    let logical_modules = ChunkLogicalModulesSummary {
        count: report.counts.final_modules,
        module_ids: report
            .final_module_contents
            .iter()
            .map(|module| module.id.clone())
            .collect(),
        target_dir: target_dir.to_string(),
    };
    let js = JsChunk {
        entry_file: target_file.clone(),
        files,
        metadata: ChunkMetadata {
            source_path: Some(source_path.clone()),
        },
    };
    let analysis = ChunkAnalysis {
        entry_file: target_file,
        files: manifest_files,
        ..base_analysis.unwrap_or_else(|| ChunkAnalysis {
            chunk_id: chunk_name,
            source_path,
            parser: Default::default(),
            entry_file: String::new(),
            counts: Default::default(),
            files: Vec::new(),
            imports: Vec::new(),
            export_aliases: Vec::new(),
            unresolved_exports: Vec::new(),
            kept_top_level_declarations: Vec::new(),
        })
    };

    let decomposition = ChunkDecompositionOutput {
        logical_modules,
        selected_module_lowerings: applied,
    };
    (
        ChunkArtifact {
            chunk_id,
            js,
            analysis,
        },
        decomposition,
    )
}
