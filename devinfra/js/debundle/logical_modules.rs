use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fs;
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use anyhow::{Context, Result, bail};
use rayon::prelude::*;
use serde::Serialize;
use swc_common::{DUMMY_SP, EqIgnoreSpan, SyntaxContext};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitMut, VisitMutWith, VisitWith};

use analysis::{
    AtomicUnitConflictReport, BindingKind, BindingName, DepKind,
    LogicalModule as ScheduleLogicalModule, LogicalModuleIndex, ModuleId, OwnerId,
    RedundantPurityHint, RedundantPurityReason, Schedule, StatementFacts,
    analyze_chunk_with_source_locations, build_owner_graph, compute_atomic_units,
    render_atomic_unit_conflict_summary, render_cycle_summary,
};
use artifact::{
    ArtifactIndexes, ArtifactSourceImportResolver, ChunkArtifact, ChunkFileRecord, ChunkId,
    ChunkLogicalModulesSummary, ChunkManifest, ChunkMetadata, ChunkTable, FileMetadata, FileRole,
    JsChunk, JsFile, JsFileBody, JsPipelineArtifact, ModuleExtractionState,
    RootLogicalModulesSummary, SelectedModuleLowering, get_chunk_entry_path, join_module_path,
    manifest_relative_path, module_path_dirname, module_path_from_path, normalize_module_path,
    relative_module_path,
};
use js_ast::{ParsedJsModule, set_str_value, str_value};
use spec::{BindingSourceKind, ChunkRenames, LogicalModule, MemberPurity, UnassignedMode};

const LOWERING_FILE_PRAGMA: &str =
    "// @ducktape-generated kind=lowerer-helper stage=selected_module_lowering ignore=detectors";
const LOWERING_GENERATOR_HEADER: &str = "// @ducktape-generator selected_module_lowering";

macro_rules! time_phase {
    ($timings:expr, $name:expr, $body:block) => {{
        let phase_started = Instant::now();
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
    pub artifact: JsPipelineArtifact,
    pub manifest: LogicalModuleManifest,
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

#[derive(Debug, Clone)]
struct LogicalRequest {
    id: String,
    target_path: String,
    residual: bool,
    members: Vec<MemberRequest>,
    /// Verbatim source of each anonymous-statement member the spec
    /// asked to co-move into this module. Resolved later (after AST
    /// analysis) into [`ModulePlan::anonymous_statement_ordinals`].
    anonymous_match_sources: Vec<String>,
}

#[derive(Debug, Clone)]
struct MemberRequest {
    binding: String,
    export_name: String,
    /// When `true`, the member's source is an import specifier in the
    /// source chunk (not a top-level decl). The materializer looks up
    /// the import statement by `binding` in the chunk body and rewrites
    /// it to a re-import in the destination module.
    is_import_specifier: bool,
    /// Spec-level purity annotation. `Pure` asserts that calls to the
    /// bound function have no observable side effects — the validator
    /// trusts the annotation and drops S edges for `<binding>(...)`
    /// call sites. `Default` means "not annotated, fall back to
    /// inferred classification". An author-trust contract; see
    /// AGENTS.md "Declared purity" and DESIGN.md A9.
    purity: MemberPurity,
}

#[derive(Debug, Clone)]
struct TopLevelDecl {
    ordinal: usize,
    names: Vec<String>,
    exported: bool,
}

#[derive(Debug, Clone)]
struct ModulePlan {
    id: String,
    target_file: String,
    /// Logical module path the spec asked for (e.g. `"ai/mcp/foo"`).
    /// Distinct from `target_file`, which is the chunk-relative
    /// emitted file path (e.g. `"modules/foo.js"`).
    target_path: String,
    explicit: bool,
    /// Local-name → public-export-name for every owned binding this
    /// plan claims (i.e. members whose `selector.binding.kind` is
    /// _not_ `ImportSpecifier`). ImportSpecifier-bound members live
    /// in `Schedule.bindings` as `BindingKind::Imported` and their
    /// emit is driven from there. Iteration order is undefined;
    /// emit / report sites sort by local name before consuming so
    /// the emitted source and JSON shapes stay deterministic.
    bindings: HashMap<String, String>,
    /// Source-chunk statement ordinals of anonymous-statement members
    /// claimed by this module. These owners have empty
    /// `declared_bindings`, so they can't be addressed by name —
    /// the spec resolves them by AST shape (see
    /// [`spec::LogicalModule::anonymous_statements`]). The
    /// materializer routes each such statement into this module's
    /// body in source order, alongside the named members.
    anonymous_statement_ordinals: Vec<usize>,
}

pub fn materialize_logical_modules(
    mut artifact: JsPipelineArtifact,
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

    let artifact_ref: &JsPipelineArtifact = &artifact;
    let chunk_results = selected_chunk_ids
        .par_iter()
        .map(|chunk_id| {
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
        .collect::<Result<Vec<_>>>()?;

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
    artifact = apply_materialized_logical_chunks(artifact, &target_dir, chunk_results)?;

    update_root_manifest(&mut artifact, &reports, &applied);
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
        fs::write(
            summary_path,
            serde_json::to_string_pretty(&manifest)? + "\n",
        )?;
    }
    Ok(MaterializeLogicalModulesResult { artifact, manifest })
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
    artifact: &'a JsPipelineArtifact,
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
    let chunk_unassigned_mode = unassigned_mode.get(chunk_id).cloned().unwrap_or_default();
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
    let header_lines = runtime_file.header_lines.clone();
    let source_path = runtime_file
        .metadata
        .source_path
        .clone()
        .or_else(|| artifact.chunk_source_path(chunk_id_interned))
        .unwrap_or_else(|| format!("{chunk_id}.js"));
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
    let mut bindings_catalogue = HashMap::<BindingName, BindingKind>::new();
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
        let module_id = ModuleId::Logical(LogicalModuleIndex(index));
        for member in &request.members {
            if let Some(existing_kind) = bindings_catalogue.get(&member.binding) {
                let existing_id = match existing_kind {
                    BindingKind::Owned {
                        owner: ModuleId::Logical(LogicalModuleIndex(owner_index)),
                    } => module_plans
                        .get(*owner_index)
                        .map(|plan| plan.id.clone())
                        .unwrap_or_else(|| format!("<plan#{owner_index}>")),
                    BindingKind::Owned { owner } => format!("{owner:?}"),
                    BindingKind::Imported {
                        re_exporter: ModuleId::Logical(LogicalModuleIndex(re_index)),
                        ..
                    } => module_plans
                        .get(*re_index)
                        .map(|plan| plan.id.clone())
                        .unwrap_or_else(|| format!("<plan#{re_index}>")),
                    BindingKind::Imported { re_exporter, .. } => format!("{re_exporter:?}"),
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
                    member.binding.clone(),
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
                bindings_catalogue.insert(binding.clone(), BindingKind::Owned { owner: module_id });
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
        let owner_id = ModuleId::Logical(LogicalModuleIndex(owner_index));
        for sibling in sibling_set {
            if sibling == claimed_name {
                continue;
            }
            match binding_assignment.get(sibling).copied() {
                None => {
                    binding_assignment.insert(sibling.clone(), owner_index);
                    bindings_catalogue
                        .insert(sibling.clone(), BindingKind::Owned { owner: owner_id });
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
        let residual_module_id = ModuleId::Logical(LogicalModuleIndex(residual_index));
        let mut residual_bindings = HashMap::<String, String>::new();
        for decl in &declarations {
            for name in &decl.names {
                if !binding_assignment.contains_key(name) {
                    binding_assignment.insert(name.clone(), residual_index);
                    residual_bindings.insert(name.clone(), name.clone());
                    bindings_catalogue.insert(
                        name.clone(),
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
            let owner_id = ModuleId::Logical(LogicalModuleIndex(owner_index));
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
                        bindings_catalogue
                            .insert(name.clone(), BindingKind::Owned { owner: owner_id });
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
        // `purity: pure` hints carried on any spec entry form
        // (logical-module member, chunk_renames member) propagate
        // the same way: add the binding's local name to
        // `declared_pure` so `classify_callee_call` returns `Pure`
        // for matching call sites. The author-trust contract is the
        // same regardless of where the entry lives. See AGENTS.md
        // "Declared purity".
        let declared_pure: BTreeSet<String> = time_phase!(timings, "collect_declared_pure", {
            let mut set = BTreeSet::new();
            for req in &explicit_requests {
                for m in &req.members {
                    if m.purity == MemberPurity::Pure {
                        set.insert(m.binding.clone());
                    }
                }
            }
            if let Some(cr) = chunk_renames.get(chunk_id) {
                for m in &cr.members {
                    if m.purity == MemberPurity::Pure {
                        set.insert(m.selector.binding.name.clone());
                    }
                }
            }
            set
        });
        let line_index = time_phase!(timings, "build_source_line_index", {
            runtime_ast.line_index()
        });
        let analysis = time_phase!(timings, "analyze_chunk_facts", {
            analyze_chunk_with_source_locations(
                &runtime_ast.module,
                &declared_pure,
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
        if let Some(ord) = analysis.top_level_await {
            anyhow::bail!(
                "materialize_logical_modules: chunk {chunk_id} has top-level `await` \
                 at statement #{ordinal} (TLA); the debundler's realizability theorem \
                 does not cover async modules (DESIGN.md A2). Wrap the awaited code \
                 in an async function or rewrite as a synchronous initialization.",
                ordinal = ord.0,
            );
        }
        if matches!(chunk_unassigned_mode, UnassignedMode::MiniFactors) {
            time_phase!(timings, "synthesize_mini_factor_plans", {
                synthesize_mini_factor_plans(
                    &analysis.facts,
                    &runtime_ast.module.body,
                    residual_plan_index,
                    &mut module_plans,
                    &mut binding_assignment,
                    &mut bindings_catalogue,
                    &mut anonymous_ordinal_assignment,
                    target_dir,
                )
            })?;
        }
        let logical_modules: Vec<ScheduleLogicalModule> =
            time_phase!(timings, "project_schedule_modules", {
                module_plans
                    .iter()
                    .map(|plan| ScheduleLogicalModule {
                        id: plan.id.clone(),
                        target_file: plan.target_file.clone(),
                        residual: !plan.explicit,
                        rename_map: plan.bindings.clone(),
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
        let redundant_purity_hints = analysis.redundant_purity_hints;
        let schedule = time_phase!(timings, "build_schedule", {
            Schedule::build(
                chunk_id.to_string(),
                analysis.facts,
                bindings_catalogue,
                logical_modules,
                chunk_renames_map.clone(),
            )
            .with_pre_existing_entry_exports(pre_existing_entry_exports.clone())
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
        let summary = render_atomic_unit_conflict_summary(&schedule_report.atomic_unit_conflicts);
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

fn apply_materialized_logical_chunks(
    mut artifact: JsPipelineArtifact,
    target_dir: &str,
    chunks: Vec<MaterializedLogicalChunk>,
) -> Result<JsPipelineArtifact> {
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

    let source_chunks = std::mem::take(&mut artifact.chunks);
    let mut output_chunks = Vec::with_capacity(source_chunks.len() + replacements.len());
    for chunk_artifact in source_chunks {
        if let Some(replacement) = replacements.remove(&chunk_artifact.chunk_id) {
            output_chunks.push(materialized_chunk_artifact(
                target_dir,
                &chunk_table,
                Some(chunk_artifact.manifest),
                replacement,
            ));
        } else {
            output_chunks.push(chunk_artifact);
        }
    }
    for replacement in replacements.into_values() {
        output_chunks.push(materialized_chunk_artifact(
            target_dir,
            &chunk_table,
            None,
            replacement,
        ));
    }
    artifact.chunks = output_chunks;
    Ok(artifact)
}

fn materialized_chunk_artifact(
    target_dir: &str,
    chunk_table: &ChunkTable,
    base_manifest: Option<ChunkManifest>,
    chunk: MaterializedLogicalChunk,
) -> ChunkArtifact {
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
    let module_extraction_state = ModuleExtractionState {
        runtime_file: target_file.clone(),
        target_dir: target_dir.to_string(),
    };
    let manifest_files = file_records
        .iter()
        .map(|(file, role)| ChunkFileRecord {
            file: file.clone(),
            role: *role,
        })
        .collect();
    let logical_modules = Some(ChunkLogicalModulesSummary {
        count: report.counts.final_modules,
        module_ids: report
            .final_module_contents
            .iter()
            .map(|module| module.id.clone())
            .collect(),
        target_dir: target_dir.to_string(),
    });
    let js = JsChunk {
        entry_file: target_file.clone(),
        files,
        metadata: ChunkMetadata {
            source_path: Some(source_path.clone()),
            module_extraction_state: Some(module_extraction_state),
        },
    };
    let mut manifest = base_manifest.unwrap_or_else(|| ChunkManifest {
        chunk_id: chunk_name,
        source_path,
        parser: Default::default(),
        entry_file: target_file.clone(),
        counts: Default::default(),
        files: Vec::new(),
        imports: Vec::new(),
        export_aliases: Vec::new(),
        unresolved_exports: Vec::new(),
        kept_top_level_declarations: Vec::new(),
        logical_modules: None,
        selected_module_lowerings: None,
        output_metrics: None,
    });
    manifest.entry_file = target_file;
    manifest.files = manifest_files;
    manifest.logical_modules = logical_modules;
    manifest.selected_module_lowerings = Some(applied);

    ChunkArtifact {
        chunk_id,
        js,
        manifest,
    }
}

struct LoweredChunk {
    files: Vec<JsFile>,
    file_records: Vec<(String, FileRole)>,
    applied: Vec<SelectedModuleLowering>,
    timings: PhaseTimings,
}

struct LowerChunkInputs<'a> {
    artifact: &'a JsPipelineArtifact,
    artifact_indexes: &'a ArtifactIndexes,
    runtime_ast: &'a ParsedJsModule,
    header_lines: &'a [String],
    entry_file: &'a str,
    chunk_id: &'a str,
    source_path: &'a str,
    declarations: &'a [TopLevelDecl],
    declaration_by_name: &'a BTreeMap<String, usize>,
    module_plans: &'a [ModulePlan],
    binding_assignment: &'a BTreeMap<String, usize>,
    /// Top-level statement ordinal → module_plan index for owners
    /// the spec claimed as anonymous-statement members. See
    /// `ModulePlan::anonymous_statement_ordinals`.
    anonymous_ordinal_assignment: &'a BTreeMap<usize, usize>,
    schedule: &'a Schedule,
    runtime_import_facts: &'a RuntimeImportFacts,
    /// In-place renames from `TransformSpec::chunk_renames`. Applied
    /// to bindings staying in entry's body — i.e. those *not* in
    /// `binding_assignment`. Bindings claimed by a logical module
    /// take their rename from the module plan; entries here for
    /// those bindings are silently dropped. Iteration order is
    /// undefined; the validation pass sorts by binding name before
    /// iterating so any spec errors are deterministic.
    chunk_renames: &'a HashMap<String, String>,
}

#[derive(Debug, Default)]
struct ModuleBodyFacts {
    imported_locals: BTreeSet<String>,
    provided_locals: BTreeSet<String>,
    referenced_idents: BTreeSet<String>,
}

struct RuntimeImportFacts {
    imports: BTreeMap<String, RuntimeImportInfo>,
}

#[derive(Debug)]
struct ImportedReexport {
    local: String,
    imported_name: String,
    imported_from: String,
    public_name: String,
}

#[derive(Default)]
struct ModuleReferenceNeeds<'a> {
    cross_module_imports_by_provider: BTreeMap<usize, BTreeMap<String, String>>,
    residual_entry_imports: BTreeMap<String, String>,
    missing_residual_exports: BTreeSet<String>,
    runtime_reimports: BTreeMap<String, &'a RuntimeImportInfo>,
}

type SourceImportResolutionKey = (String, String, String);
type SourceImportResolution = Option<(String, String, String)>;

struct ArtifactSourceImportResolutionCache<'a> {
    artifact: &'a JsPipelineArtifact,
    indexes: &'a ArtifactIndexes,
    resolutions: BTreeMap<SourceImportResolutionKey, SourceImportResolution>,
    resolver: Option<ArtifactSourceImportResolver<'a>>,
}

impl<'a> ArtifactSourceImportResolutionCache<'a> {
    fn new(artifact: &'a JsPipelineArtifact, indexes: &'a ArtifactIndexes) -> Self {
        Self {
            artifact,
            indexes,
            resolutions: BTreeMap::new(),
            resolver: None,
        }
    }

    fn resolve(
        &mut self,
        source: &str,
        caller_chunk_id: &str,
        caller_file: &str,
    ) -> Result<SourceImportResolution> {
        if source.is_empty() || (!source.starts_with('.') && !source.starts_with('/')) {
            return Ok(None);
        }
        let key = (
            source.to_string(),
            caller_chunk_id.to_string(),
            caller_file.to_string(),
        );
        if let Some(resolved) = self.resolutions.get(&key) {
            return Ok(resolved.clone());
        }
        if self.resolver.is_none() {
            self.resolver = Some(self.artifact.source_import_resolver(self.indexes));
        }
        let caller_chunk_id_interned = self
            .artifact
            .chunk_table
            .get(caller_chunk_id)
            .with_context(|| format!("unknown caller chunk: {caller_chunk_id}"))?;
        let resolved = self
            .resolver
            .as_ref()
            .expect("resolver initialized")
            .resolve(source, caller_chunk_id_interned, caller_file)?;
        self.resolutions.insert(key, resolved.clone());
        Ok(resolved)
    }
}

fn lower_chunk(inputs: LowerChunkInputs<'_>) -> Result<LoweredChunk> {
    let LowerChunkInputs {
        artifact,
        artifact_indexes,
        runtime_ast,
        header_lines,
        entry_file,
        chunk_id,
        source_path,
        declarations,
        declaration_by_name,
        module_plans,
        binding_assignment,
        anonymous_ordinal_assignment,
        schedule,
        runtime_import_facts,
        chunk_renames,
    } = inputs;
    let mut timings = PhaseTimings::default();
    let selected_ordinals = time_phase!(timings, "compute_selected_ordinals", {
        let mut selected_ordinals = BTreeSet::new();
        for decl in declarations {
            if decl
                .names
                .iter()
                .any(|name| binding_assignment.contains_key(name))
            {
                selected_ordinals.insert(decl.ordinal);
            }
        }
        for ordinal in anonymous_ordinal_assignment.keys() {
            selected_ordinals.insert(*ordinal);
        }
        selected_ordinals
    });

    let mut selected_by_module = vec![Vec::<ModuleItem>::new(); module_plans.len()];
    let mut selected_exports_by_module =
        vec![Option::<BTreeMap<String, String>>::None; module_plans.len()];
    time_phase!(timings, "plan_selected_exports", {
        for (module_index, plan) in module_plans.iter().enumerate() {
            if plan.bindings.is_empty() {
                continue;
            }
            // Drop bindings that don't exist anywhere (no entry in
            // `binding_assignment`). Without this, a stale spec entry
            // for a binding that is not a top-level decl in the chunk
            // would emit `export { <renamed> }` with no backing decl
            // and Node bails at module load with `SyntaxError: Export
            // '<renamed>' is not defined in module`.
            let exports: BTreeMap<String, String> = plan
                .bindings
                .iter()
                .filter(|(name, _)| binding_assignment.contains_key(*name))
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect();
            if !exports.is_empty() {
                selected_exports_by_module[module_index] = Some(exports);
            }
        }
    });

    let mut entry_body = Vec::new();
    let import_insert_index = runtime_ast
        .module
        .body
        .iter()
        .take_while(|item| matches!(item, ModuleItem::ModuleDecl(ModuleDecl::Import(_))))
        .count();
    time_phase!(timings, "split_entry_body", {
        for (ordinal, item) in runtime_ast.module.body.iter().enumerate() {
            if !selected_ordinals.contains(&ordinal) {
                entry_body.push(item.clone());
                continue;
            }
            // Anonymous-statement members route the entire item to
            // the claiming module's body — no per-binding splitting.
            if let Some(module_index) = anonymous_ordinal_assignment.get(&ordinal).copied() {
                selected_by_module[module_index].push(item.clone());
                continue;
            }
            let mut remaining =
                remaining_item_after_selection(item, binding_assignment, &mut selected_by_module)?;
            entry_body.append(&mut remaining);
        }
        Ok::<_, anyhow::Error>(())
    })?;
    // Two passes: build entry imports in plan order (so the
    // first plan to claim a binding wins disambiguation), then
    // sort the resulting imports by `linker_order` so ECMA-262's
    // depth-first link traversal evaluates dependencies first.
    // Plan-order disambiguation + linker-order placement keeps
    // the import-collision contract while satisfying Lemma 2's
    // emit-side constraint. See DESIGN.md "Module dep graphs"
    // and "Lemma 2".
    let build_entry_imports_started = Instant::now();
    let mut entry_imports: Vec<(usize, ModuleItem)> = Vec::new();
    let mut occupied = collect_occupied_local_names(&entry_body);
    let mut body_renames = BTreeMap::<String, String>::new();
    // Seed body_renames with `chunk_renames` entries for bindings
    // staying in entry's body (not claimed by any logical module).
    // Bindings owned by a logical module take their rename from the
    // module plan via the disambiguate-imports pass below;
    // chunk_renames entries for those bindings are silently
    // dropped here (the logical-module rename wins).
    //
    // Each accepted target name is reserved in `occupied` before the
    // import-disambiguation pass runs, so a later cross-module
    // import doesn't mint a fresh local that collides with one of
    // the chunk_renames' targets. Conflicting targets (target name
    // already taken by a body local that isn't being renamed away,
    // or by another chunk_renames entry, or invalid as an
    // identifier) bail rather than producing invalid JS silently.
    let mut renamed_away = BTreeSet::<String>::new();
    for binding in chunk_renames.keys() {
        if binding_assignment.contains_key(binding) {
            continue;
        }
        renamed_away.insert(binding.clone());
    }
    // Iterate `chunk_renames` (a `HashMap`) in sorted order so the
    // collected error list and the `body_renames` insertion order
    // are stable. Collect every violation rather than `bail!`ing on
    // the first one so a spec author sees the full set in one
    // round-trip; the "duplicate target" branch in particular only
    // surfaces after `occupied.insert` returned false, so the
    // earlier-rename whose target was duplicated is implied by the
    // sort order.
    let mut sorted_renames: Vec<(&String, &String)> = chunk_renames.iter().collect();
    sorted_renames.sort_by(|a, b| a.0.cmp(b.0));
    let mut errors = Vec::<String>::new();
    for (binding, export_name) in sorted_renames {
        if binding_assignment.contains_key(binding) {
            continue;
        }
        if !is_valid_js_identifier(export_name) {
            errors.push(format!(
                "chunk_renames target {export_name} for binding {binding} is not a valid JS identifier",
            ));
            continue;
        }
        if export_name != binding {
            // A body local that's also being renamed away vacates
            // its slot in `occupied` — it's safe to reuse. Anything
            // else still in `occupied` would collide.
            let target_already_taken =
                occupied.contains(export_name) && !renamed_away.contains(export_name);
            if target_already_taken {
                errors.push(format!(
                    "chunk_renames target {export_name} for binding {binding} collides with an existing top-level local",
                ));
                continue;
            }
        }
        if !occupied.insert(export_name.clone()) && export_name != binding {
            // `occupied.insert` returns false if already present;
            // for the rename-to-self case (export_name == binding)
            // that's expected. For any other case the target was
            // already chosen by a previous chunk_renames entry —
            // duplicate target.
            errors.push(format!(
                "chunk_renames target {export_name} for binding {binding} duplicates an earlier rename target",
            ));
            continue;
        }
        body_renames.insert(binding.clone(), export_name.clone());
    }
    if !errors.is_empty() {
        bail!("invalid chunk_renames spec:\n  - {}", errors.join("\n  - "));
    }
    occupied.extend(collect_local_binding_names(&entry_body));
    for (module_index, plan) in module_plans.iter().enumerate() {
        if plan.bindings.is_empty() {
            continue;
        }
        // Drop bindings that don't exist anywhere (no entry in
        // `binding_assignment`). Bindings owned by another plan stay
        // in the import — they're a separate "two plans claim the
        // same binding" disambiguation case handled by
        // `disambiguate_import_locals`.
        let live_bindings: BTreeMap<String, String> = plan
            .bindings
            .iter()
            .filter(|(name, _)| binding_assignment.contains_key(*name))
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        if live_bindings.is_empty() {
            continue;
        }
        let mut emit_renames = BTreeMap::<String, String>::new();
        let resolved = disambiguate_import_locals(&live_bindings, &mut occupied, &mut emit_renames);
        // A rename only propagates to consumer-body references when the
        // moved decl actually belongs to this plan. Plans that listed a
        // binding without owning the decl emit a dangling import; the
        // body refs continue to resolve to whichever binding owned the
        // original local name.
        for (local, fresh) in emit_renames {
            if binding_assignment.get(&local).copied() == Some(module_index) {
                body_renames.insert(local, fresh);
            }
        }
        entry_imports.push((
            module_index,
            import_decl_for_plan(entry_file, &plan.target_file, &resolved),
        ));
    }
    // Sort the (plan-order-disambiguated) imports by linker_order
    // so the first import in the entry source corresponds to the
    // earliest-in-L provider. Stable sort preserves plan-order for
    // ties (e.g. when two providers have no dep-graph relation).
    entry_imports.sort_by_key(|(idx, _)| {
        schedule
            .linker_position(ModuleId::Logical(LogicalModuleIndex(*idx)))
            .unwrap_or(usize::MAX)
    });
    let entry_imports: Vec<ModuleItem> = entry_imports.into_iter().map(|(_, it)| it).collect();
    timings.add("build_entry_imports", build_entry_imports_started.elapsed());
    let entry_binding_renames = body_renames.clone();
    if !body_renames.is_empty() {
        let rename_entry_body_started = Instant::now();
        // Re-exports `export { local }` (without `from`) collapse `local`
        // and the public exported name into a single ident. Renaming the
        // orig would also rename the public name, breaking downstream
        // consumers — so rewrite them to `export { fresh as local }`
        // before the generic renamer visits the rest.
        for item in entry_body.iter_mut() {
            preserve_export_specifier_names(item, &body_renames);
        }
        let mut renamer = IdentifierRenamer {
            renames: &body_renames,
        };
        for item in entry_body.iter_mut() {
            item.visit_mut_with(&mut renamer);
        }
        timings.add("rename_entry_body", rename_entry_body_started.elapsed());
    }
    if !entry_imports.is_empty() {
        let splice_entry_imports_started = Instant::now();
        let tail = entry_body.split_off(import_insert_index);
        entry_body.extend(entry_imports);
        entry_body.extend(tail);
        timings.add(
            "splice_entry_imports",
            splice_entry_imports_started.elapsed(),
        );
    }
    time_phase!(timings, "entry_exports_and_trim", {
        for export in entry_exports_for_moved_bindings(
            declarations,
            binding_assignment,
            &entry_binding_renames,
        ) {
            entry_body.push(export);
        }
        trim_dead_named_specifiers(&mut entry_body, &schedule.bindings);
    });
    let entry_exports_by_original_local = time_phase!(timings, "collect_entry_exports", {
        collect_entry_exports_by_original_local(&entry_body, &entry_binding_renames)
    });
    let imported_reexports_by_module = time_phase!(timings, "collect_imported_reexports", {
        collect_imported_reexports_by_module(schedule, module_plans.len())
    });
    let mut source_import_cache =
        ArtifactSourceImportResolutionCache::new(artifact, artifact_indexes);

    let mut files = vec![JsFile {
        path: entry_file.to_string(),
        body: JsFileBody::Ast(ParsedJsModule {
            cm: runtime_ast.cm.clone(),
            module: Module {
                span: DUMMY_SP,
                body: entry_body,
                shebang: None,
            },
        }),
        header_lines: header_lines.to_vec(),
        metadata: FileMetadata {
            chunk_id: Some(chunk_id.to_string()),
            chunk_file: Some(entry_file.to_string()),
            role: Some(FileRole::Entry),
            source_path: Some(source_path.to_string()),
            ..Default::default()
        },
    }];
    let mut file_records = vec![(entry_file.to_string(), FileRole::Entry)];
    let mut applied = Vec::new();

    // Filter chunk_renames down to entries the per-module emit path
    // should apply: bindings *not* claimed by any logical module.
    // Claimed bindings get their rename from the module plan
    // (handled via `disambiguate_import_locals` for cross-module
    // imports of the binding); the chunk_renames entry is dropped
    // for those. Mirrors the residual-side rule on body_renames
    // seeding above. The map is empty for chunks with no
    // chunk_renames; the per-module renamer is then a no-op.
    let cross_module_chunk_renames: BTreeMap<String, String> = chunk_renames
        .iter()
        .filter(|(binding, _)| !binding_assignment.contains_key(*binding))
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();

    for (index, plan) in module_plans.iter().enumerate() {
        let mut body = std::mem::take(&mut selected_by_module[index]);
        let local_renames = time_phase!(timings, "module.naturalize_body", {
            naturalize_module_body(&mut body, plan)
        });
        let body_facts = time_phase!(timings, "module.collect_body_facts", {
            collect_module_body_facts(&body)
        });
        let ModuleReferenceNeeds {
            cross_module_imports_by_provider,
            residual_entry_imports,
            missing_residual_exports,
            runtime_reimports,
        } = time_phase!(timings, "module.plan_references", {
            plan_module_reference_needs(
                index,
                &body_facts,
                schedule,
                declaration_by_name,
                binding_assignment,
                &entry_exports_by_original_local,
                runtime_import_facts,
            )
        });
        let mut module_import_renames = BTreeMap::<String, String>::new();
        let mut module_import_locals = collect_local_binding_names(&body);
        let mut module_imports = time_phase!(timings, "module.build_cross_imports", {
            cross_module_imports_for_plan(
                &plan.target_file,
                cross_module_imports_by_provider,
                schedule,
                &mut module_import_locals,
                &mut module_import_renames,
            )
        });
        let mut residual_entry_imports = time_phase!(timings, "module.build_residual_imports", {
            residual_entry_imports_for_moved_body(
                &plan.id,
                entry_file,
                &plan.target_file,
                residual_entry_imports,
                missing_residual_exports,
                &mut module_import_locals,
                &mut module_import_renames,
            )
        })?;
        if !module_import_renames.is_empty() {
            let mut renamer = IdentifierRenamer {
                renames: &module_import_renames,
            };
            for item in body.iter_mut() {
                item.visit_mut_with(&mut renamer);
            }
        }
        // Re-import any source-chunk import-specifier-bound locals that
        // moved code in `body` references but no top-level decl
        // satisfies (e.g. `const { decode } = gge;` where `gge` was an
        // ImportSpecifier in the source chunk's runtime body). Without
        // this, the moved code references a free variable and Node
        // throws `ReferenceError: gge is not defined` at runtime.
        let mut runtime_reimports = time_phase!(timings, "module.build_runtime_reimports", {
            source_chunk_imports_for_moved_body(
                &mut source_import_cache,
                chunk_id,
                entry_file,
                &plan.target_file,
                runtime_reimports,
            )
        })?;
        module_imports.append(&mut residual_entry_imports);
        module_imports.append(&mut runtime_reimports);
        module_imports.append(&mut body);
        body = module_imports;
        // Apply chunk_renames to the assembled module body so that
        // import-specifier aliases and references in the moved code
        // both pick up the spec's rename. Without this, residual
        // entry says `getMobxGlobalState` but the peeled module's
        // `import { f as cx }` and `cx()` refs still say `cx`,
        // producing two disagreeing local aliases for the same
        // upstream binding.
        if !cross_module_chunk_renames.is_empty() {
            time_phase!(timings, "module.rename_chunk_renames", {
                let mut renamer = IdentifierRenamer {
                    renames: &cross_module_chunk_renames,
                };
                for item in body.iter_mut() {
                    item.visit_mut_with(&mut renamer);
                }
            });
        }
        time_phase!(timings, "module.rewrite_runtime_sources", {
            rewrite_runtime_sources_for_target(&mut body, &plan.target_file);
        });
        // ImportSpecifier-bound members (`BindingKind::Imported` in
        // `schedule.bindings`): for each `Imported` binding whose
        // `re_exported_by` map names this module, emit a re-import
        // (using the local name as the alias) plus mirror the
        // public-name export. Per-destination relative paths are
        // computed here so multiple modules at different output
        // depths each get a correctly-relativised path.
        let import_member_exports = time_phase!(timings, "module.imported_reexports", {
            let mut import_member_exports = BTreeMap::<String, String>::new();
            let reexports = &imported_reexports_by_module[index];
            if !reexports.is_empty() {
                let import_count = body
                    .iter()
                    .take_while(|item| {
                        matches!(item, ModuleItem::ModuleDecl(ModuleDecl::Import(_)))
                    })
                    .count();
                // `imported_from` on `BindingKind::Imported` is output-tree-
                // rooted absolute; `plan.target_file` is chunk-rooted. Lift
                // the destination to the same coordinate system before
                // computing the relative path.
                let dest_abs = join_module_path(&[chunk_id, &plan.target_file]);
                // Group reexports by rewritten source so multiple bindings
                // re-exported from the same import-from end up in a single
                // `import { ... } from "<src>"` statement, not one
                // statement per binding. First-occurrence order is
                // preserved for both source groups and bindings within
                // each group. All specifiers emitted here are Named, so
                // ESM's Namespace/Named mutual-exclusion rule doesn't
                // apply.
                let mut groups: Vec<(String, Vec<ImportSpecifier>)> =
                    Vec::with_capacity(reexports.len());
                let mut index_by_source: BTreeMap<String, usize> = BTreeMap::new();
                for reexport in reexports {
                    let src = relative_source(&dest_abs, &reexport.imported_from);
                    let specifier =
                        imported_binding_named_specifier(&reexport.local, &reexport.imported_name);
                    let group_index = *index_by_source.entry(src.clone()).or_insert_with(|| {
                        groups.push((src.clone(), Vec::new()));
                        groups.len() - 1
                    });
                    groups[group_index].1.push(specifier);
                    import_member_exports
                        .insert(reexport.local.clone(), reexport.public_name.clone());
                }
                let mut reexport_imports = Vec::with_capacity(groups.len());
                for (src, specifiers) in groups {
                    reexport_imports.push(import_decl_module_item(specifiers, &src));
                }
                let tail = body.split_off(import_count);
                body.extend(reexport_imports);
                body.extend(tail);
            }
            import_member_exports
        });
        time_phase!(timings, "module.final_exports", {
            if let Some(exports) = &selected_exports_by_module[index] {
                let mut exports = final_module_exports(exports, &local_renames);
                exports.extend(
                    import_member_exports
                        .iter()
                        .map(|(k, v)| (k.clone(), v.clone())),
                );
                body.push(export_named_for_bindings(&exports));
            } else if !import_member_exports.is_empty() {
                body.push(export_named_for_bindings(&import_member_exports));
            }
        });
        time_phase!(timings, "module.build_output_records", {
            // Materialize `plan.bindings` (a HashMap) in sorted order so
            // `binding_names`, `exported_names`, the header comment, and
            // the resolved `owner_ids` all share the same canonical
            // sequence regardless of hash seed.
            let mut sorted_plan_bindings: Vec<(&String, &String)> = plan.bindings.iter().collect();
            sorted_plan_bindings.sort_by(|a, b| a.0.cmp(b.0));
            let binding_names: Vec<String> = sorted_plan_bindings
                .iter()
                .map(|(k, _)| (*k).clone())
                .collect();
            let exported_names: Vec<String> = sorted_plan_bindings
                .iter()
                .map(|(_, v)| (*v).clone())
                .collect();
            let owner_ids =
                schedule.owner_report_ids_for_bindings(binding_names.iter().map(String::as_str));
            let header = vec![
                LOWERING_FILE_PRAGMA.to_string(),
                LOWERING_GENERATOR_HEADER.to_string(),
                format!(
                    "// Selected-module lowered region; original owner ids: {}.",
                    owner_ids.join(", ")
                ),
                format!(
                    "// Selected-module lowered region; source bindings: {}.",
                    binding_names.join(", ")
                ),
            ];
            files.push(JsFile {
                path: plan.target_file.clone(),
                body: JsFileBody::Ast(ParsedJsModule {
                    cm: runtime_ast.cm.clone(),
                    module: Module {
                        span: DUMMY_SP,
                        body,
                        shebang: None,
                    },
                }),
                header_lines: header,
                metadata: FileMetadata {
                    chunk_id: Some(chunk_id.to_string()),
                    chunk_file: Some(plan.target_file.clone()),
                    role: Some(FileRole::Module),
                    source_path: Some(source_path.to_string()),
                    output_path: None,
                    generated_stage: Some("selected_module_lowering".to_string()),
                },
            });
            file_records.push((plan.target_file.clone(), FileRole::Module));
            applied.push(SelectedModuleLowering {
                binding_names,
                chunk_id: chunk_id.to_string(),
                exported_names,
                file: entry_file.to_string(),
                id: plan.id.clone(),
                owner_ids,
                residual: !plan.explicit,
                target_file: plan.target_file.clone(),
                target_path: plan.target_path.clone(),
            });
        });
    }

    Ok(LoweredChunk {
        files,
        file_records,
        applied,
        timings,
    })
}

fn logical_requests_for_chunk(
    chunk_logical_modules: Option<&BTreeMap<String, LogicalModule>>,
    chunk_unassigned_mode: &UnassignedMode,
    chunk_renames_present: bool,
    chunk_id: &str,
    target_dir: &str,
) -> Result<Vec<LogicalRequest>> {
    let mut requests = Vec::new();
    let catchall_target = chunk_unassigned_mode
        .catchall_file_target()
        .map(str::to_string);
    let mut explicit_module_at_catchall = false;
    if let Some(by_target_path) = chunk_logical_modules {
        for (target_path, module) in by_target_path {
            let id = format!("{chunk_id}::{target_path}");
            let members = build_members(&module.members);
            reject_duplicate_export_names("logical_module", &id, &members)?;
            reject_duplicate_member_bindings("logical_module", &id, &members)?;
            let anonymous_match_sources = module
                .anonymous_statements
                .iter()
                .map(|stmt| stmt.match_source.clone())
                .collect();
            if catchall_target.as_deref() == Some(target_path.as_str()) {
                explicit_module_at_catchall = true;
            }
            requests.push(LogicalRequest {
                id,
                target_path: target_path.clone(),
                residual: false,
                members,
                anonymous_match_sources,
            });
        }
    }
    // Synthesize a memberless catchall-file request when the chunk's
    // `unassigned_mode` is `CatchallFile` and no explicit logical
    // module already claims the catchall target. When an explicit
    // module *is* at the catchall target, the residual sweep in
    // `materialize_logical_chunk` will append unclaimed bindings to
    // that explicit plan instead.
    if let Some(target_path) = catchall_target
        && !explicit_module_at_catchall
    {
        requests.push(LogicalRequest {
            id: format!("{chunk_id}::residual"),
            target_path,
            residual: true,
            members: Vec::new(),
            anonymous_match_sources: Vec::new(),
        });
    }
    // Fallback: when the spec is silent about this chunk (no
    // `logical_modules`, default `InlineInEntry` mode, no
    // `chunk_renames`), inject a memberless residual so the
    // materializer has at least one module to point unowned decls
    // at. Skipped when the spec has any `chunk_renames` for the
    // chunk — that signals the spec wants bindings to stay in
    // `ResidualEntry`-land (no `Logical(R)` module, no separate
    // residual file emitted), with renames applied in-place by the
    // lowerer. Skipped when `MiniFactors` is active — the
    // synthesizer takes care of placing unclaimed code into
    // mini-factor modules.
    if requests.is_empty()
        && !chunk_renames_present
        && !matches!(chunk_unassigned_mode, UnassignedMode::MiniFactors)
    {
        requests.push(LogicalRequest {
            id: format!("{chunk_id}::residual"),
            target_path: join_module_path(&[target_dir, "unhandled"]),
            residual: true,
            members: Vec::new(),
            anonymous_match_sources: Vec::new(),
        });
    }
    Ok(requests)
}

/// Collect a `ChunkRenames` block into a `binding_name -> export_name`
/// map. Bindings that appear more than once across `members` fail
/// fast — silently last-write-wins on a binding rename is the same
/// hazard as duplicate logical-module member bindings.
/// True iff `s` is a valid JavaScript identifier — start char is
/// `[A-Za-z_$]` and rest is `[A-Za-z0-9_$]`. Reserved words are not
/// rejected (a target named e.g. `class` or `let` would still trip
/// at parse time downstream, but that's a louder failure than this
/// shallow check would catch). The intent is to filter typos
/// (`with-dash`, `0digit`, empty string) from spec authors.
fn is_valid_js_identifier(s: &str) -> bool {
    let mut chars = s.chars();
    let first = match chars.next() {
        Some(c) => c,
        None => return false,
    };
    if !(first.is_ascii_alphabetic() || first == '_' || first == '$') {
        return false;
    }
    chars.all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '$')
}

fn collect_chunk_renames(chunk_renames: &ChunkRenames) -> Result<HashMap<String, String>> {
    let mut renames = HashMap::<String, String>::new();
    let id = chunk_renames.id.as_deref().unwrap_or("chunk_renames");
    for member in &chunk_renames.members {
        let binding = member.selector.binding.name.clone();
        let export_name = member.name.clone().unwrap_or_else(|| binding.clone());
        if let Some(existing) = renames.get(&binding) {
            if existing != &export_name {
                bail!(
                    "chunk_renames {id}: binding {binding} already renamed to \
                     {existing}; refusing to overwrite with {export_name}"
                );
            }
        } else {
            renames.insert(binding, export_name);
        }
    }
    Ok(renames)
}

/// Number of post-comma-list-split positions a top-level body
/// item produces. `var x = …, y = …;` is one body item but two
/// post-split owners (and therefore two `StatementOrdinal`s in
/// the owner graph). All other top-level items count as one.
/// Mirrors the splitting in `facts::top_level_item_views`.
fn post_split_top_level_count(item: &ModuleItem) -> usize {
    fn decl_count(decl: &Decl) -> usize {
        match decl {
            Decl::Var(var) if var.decls.len() > 1 => var.decls.len(),
            _ => 1,
        }
    }
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => decl_count(decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
            decl_count(&export_decl.decl)
        }
        _ => 1,
    }
}

/// Convert a pre-split body index to the first post-split
/// `StatementOrdinal` value for that body item. For anonymous
/// statements (which never split), this is the only ordinal in
/// the resulting range.
fn statement_ordinal_for_body_index(body: &[ModuleItem], body_idx: usize) -> usize {
    body[..body_idx]
        .iter()
        .map(post_split_top_level_count)
        .sum()
}

/// Inverse of [`statement_ordinal_for_body_index`]: given a post-split
/// statement ordinal, return the pre-split body index of the body item
/// that produced it. Returns `None` if the ordinal is past the body.
fn body_index_for_statement_ordinal(body: &[ModuleItem], stmt_ordinal: usize) -> Option<usize> {
    let mut running = 0usize;
    for (idx, item) in body.iter().enumerate() {
        let count = post_split_top_level_count(item);
        if stmt_ordinal < running + count {
            return Some(idx);
        }
        running += count;
    }
    None
}

/// `unassigned_mode == MiniFactors`: for each atomic factor
/// unit whose members are entirely unclaimed by the YAML spec (i.e.
/// either currently sitting in the residual catch-all or never
/// assigned to any plan), synthesize a stand-alone [`ModulePlan`]
/// containing exactly those members. Bindings and anonymous
/// statements that were temporarily routed through the residual
/// plan are moved into the synthesized plan; the residual plan then
/// only holds whatever truly couldn't be peeled (typically nothing
/// for clean chunks).
///
/// The synthesized plan's `target_path` is deterministic
/// (`__auto/mini/{idx:04}`) and indexed by the unit's position in
/// the iteration order of unclaimed units, sorted by the smallest
/// member `OwnerId` so the names are stable run-to-run.
#[allow(clippy::too_many_arguments)]
fn synthesize_mini_factor_plans(
    facts: &[StatementFacts],
    body: &[ModuleItem],
    residual_plan_index: Option<usize>,
    module_plans: &mut Vec<ModulePlan>,
    binding_assignment: &mut BTreeMap<String, usize>,
    bindings_catalogue: &mut HashMap<BindingName, BindingKind>,
    anonymous_ordinal_assignment: &mut BTreeMap<usize, usize>,
    target_dir: &str,
) -> Result<()> {
    let owner_graph = build_owner_graph(facts);
    let atomic_units = compute_atomic_units(&owner_graph);
    let mut owner_declared_names: HashMap<OwnerId, Vec<BindingName>> = HashMap::new();
    let mut owner_statement_ordinal: HashMap<OwnerId, usize> = HashMap::new();
    for node in owner_graph.iter_nodes() {
        let names: Vec<BindingName> = node
            .declared
            .iter()
            .filter_map(|bid| owner_graph.binding_table.name(*bid).cloned())
            .collect();
        owner_declared_names.insert(node.id, names);
        owner_statement_ordinal.insert(node.id, node.statement_ordinal.0);
    }

    // A unit member counts as unclaimed iff every declared binding is
    // either absent from `binding_assignment` or assigned to the
    // residual plan (if any); anonymous owners must similarly be
    // unassigned or routed via residual. If any member is claimed by
    // an explicit (non-residual) plan, the spec author already named
    // the unit's destination — leave the existing claim intact (and
    // let downstream validation flag an atomic-unit conflict if the
    // claims disagree).
    let is_owner_unclaimed = |owner: OwnerId| -> bool {
        let names = owner_declared_names
            .get(&owner)
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        for name in names {
            match binding_assignment.get(name).copied() {
                None => continue,
                Some(idx) if Some(idx) == residual_plan_index => continue,
                Some(_) => return false,
            }
        }
        if names.is_empty() {
            let Some(stmt_ord) = owner_statement_ordinal.get(&owner).copied() else {
                return true;
            };
            let Some(body_idx) = body_index_for_statement_ordinal(body, stmt_ord) else {
                return true;
            };
            match anonymous_ordinal_assignment.get(&body_idx).copied() {
                None => return true,
                Some(idx) if Some(idx) == residual_plan_index => return true,
                Some(_) => return false,
            }
        }
        true
    };

    let mut unclaimed_units: Vec<&BTreeSet<OwnerId>> = atomic_units
        .iter()
        .filter(|unit| unit.members.iter().copied().all(is_owner_unclaimed))
        .map(|unit| &unit.members)
        .collect();
    // Stable iteration order: smallest OwnerId first.
    unclaimed_units.sort_by_key(|members| members.iter().next().copied());

    for (idx, members) in unclaimed_units.into_iter().enumerate() {
        let synthetic_idx = module_plans.len();
        let synthetic_module_id = ModuleId::Logical(LogicalModuleIndex(synthetic_idx));
        let target_path = format!("__auto/mini/{idx:04}");
        let target_file = target_file_for_request(target_dir, &target_path)?;
        let mut bindings = HashMap::<String, String>::new();
        let mut anonymous_statement_ordinals = Vec::<usize>::new();
        for owner in members {
            let names = owner_declared_names
                .get(owner)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            if names.is_empty() {
                let Some(stmt_ord) = owner_statement_ordinal.get(owner).copied() else {
                    continue;
                };
                let Some(body_idx) = body_index_for_statement_ordinal(body, stmt_ord) else {
                    continue;
                };
                anonymous_ordinal_assignment.insert(body_idx, synthetic_idx);
                anonymous_statement_ordinals.push(body_idx);
                continue;
            }
            for name in names {
                bindings.insert(name.clone(), name.clone());
                // Move the binding out of the residual plan (if it was
                // staged there by the sweep above) into the synthesized
                // plan. The residual plan's bindings/anonymous-ordinal
                // maps are pruned so it doesn't double-claim members.
                if let Some(prev) = binding_assignment.get(name).copied() {
                    if Some(prev) == residual_plan_index {
                        if let Some(residual_idx) = residual_plan_index {
                            module_plans[residual_idx].bindings.remove(name);
                        }
                    }
                }
                binding_assignment.insert(name.clone(), synthetic_idx);
                bindings_catalogue.insert(
                    name.clone(),
                    BindingKind::Owned {
                        owner: synthetic_module_id,
                    },
                );
            }
        }
        anonymous_statement_ordinals.sort_unstable();
        module_plans.push(ModulePlan {
            id: target_path.clone(),
            target_file,
            target_path,
            explicit: false,
            bindings,
            anonymous_statement_ordinals,
        });
    }
    Ok(())
}

/// Resolve every `anonymous_match_sources` entry on `request` to a
/// pre-split body index in `runtime_module`'s top-level body. The
/// resolver requires exactly one match per entry — a 0-match or
/// ambiguous-match selector is a spec error.
fn resolve_anonymous_statement_ordinals(
    request: &LogicalRequest,
    runtime_module: &Module,
) -> Result<Vec<usize>> {
    let mut resolved = Vec::with_capacity(request.anonymous_match_sources.len());
    for match_source in &request.anonymous_match_sources {
        let parsed = js_ast::parse_js_module_ast(
            &format!("<anonymous_statement match in {}>", request.id),
            match_source,
        )
        .with_context(|| {
            format!(
                "logical_module {}: anonymous_statements[].match did not parse as JS:\n{match_source}",
                request.id
            )
        })?;
        let parsed_items: Vec<&ModuleItem> = parsed.body.iter().collect();
        let needle = match parsed_items.as_slice() {
            [single] => *single,
            [] => bail!(
                "logical_module {}: anonymous_statements[].match parsed to zero \
                 statements; selector source must contain exactly one top-level \
                 statement:\n{match_source}",
                request.id,
            ),
            _ => bail!(
                "logical_module {}: anonymous_statements[].match parsed to {} \
                 statements; selector source must contain exactly one top-level \
                 statement:\n{match_source}",
                request.id,
                parsed_items.len(),
            ),
        };
        let matches: Vec<usize> = runtime_module
            .body
            .iter()
            .enumerate()
            .filter_map(|(ordinal, item)| {
                if needle.eq_ignore_span(item) {
                    Some(ordinal)
                } else {
                    None
                }
            })
            .collect();
        match matches.as_slice() {
            [single] => resolved.push(*single),
            [] => bail!(
                "logical_module {}: anonymous_statements[].match did not match any \
                 top-level statement in the chunk. Selector:\n{match_source}",
                request.id,
            ),
            multiple => bail!(
                "logical_module {}: anonymous_statements[].match is ambiguous — \
                 matched {} top-level statements at ordinals {:?}. Refine the \
                 selector. Source:\n{match_source}",
                request.id,
                multiple.len(),
                multiple,
            ),
        }
    }
    Ok(resolved)
}

fn build_members(members: &[spec::Member]) -> Vec<MemberRequest> {
    members
        .iter()
        .map(|m| {
            let binding = m.selector.binding.name.clone();
            let export_name = m.name.clone().unwrap_or_else(|| binding.clone());
            MemberRequest {
                is_import_specifier: matches!(
                    m.selector.binding.kind,
                    Some(BindingSourceKind::ImportSpecifier)
                ),
                binding,
                export_name,
                purity: m.purity,
            }
        })
        .collect()
}

struct ChunkAstAnalysis {
    runtime_import_facts: RuntimeImportFacts,
    declarations: Vec<TopLevelDecl>,
    declaration_by_name: BTreeMap<String, usize>,
    /// Sibling sets for top-level destructuring declarators only.
    /// For a destructuring declarator like `const { x, y } = obj`
    /// both `x` and `y` map to the set `{x, y}`. Plain
    /// single-name declarators (`const a = 1`) are not recorded —
    /// they don't need atomicity enforcement, and absence here is
    /// the signal that there are no siblings to consider. Used by
    /// `build_module_plans` to enforce destructure-atomicity:
    /// claiming any one binding from a destructure pulls the rest
    /// into the same module, because the materializer's
    /// `split_var_decl` moves a destructuring declarator as one
    /// atomic unit.
    destructure_siblings: BTreeMap<String, BTreeSet<String>>,
    /// Names that the source chunk's entry already exports (via
    /// `export { foo, bar }` re-exports of local bindings, or
    /// `export const foo = …` style declarations). Passed to
    /// [`Schedule::with_pre_existing_entry_exports`] so
    /// peelability's emit-resolvability projection can predict
    /// the materializer's "moved module references residual entry
    /// binding(s) … not exported by entry" rejection without
    /// re-walking the AST.
    pre_existing_entry_exports: BTreeSet<String>,
}

fn analyze_chunk_ast(module: &Module) -> ChunkAstAnalysis {
    let mut imports = BTreeMap::<String, RuntimeImportInfo>::new();
    let mut declarations = Vec::new();
    let mut pre_existing_entry_exports = BTreeSet::<String>::new();
    let mut destructure_siblings = BTreeMap::<String, BTreeSet<String>>::new();
    for (ordinal, item) in module.body.iter().enumerate() {
        let (names, exported) = top_level_declaration_names(item);
        if !names.is_empty() {
            if exported {
                pre_existing_entry_exports.extend(names.iter().cloned());
            }
            declarations.push(TopLevelDecl {
                ordinal,
                names,
                exported,
            });
        }
        record_destructure_sibling_groups(item, &mut destructure_siblings);
        record_runtime_imports(item, &mut imports);
        record_pre_existing_named_exports(item, &mut pre_existing_entry_exports);
    }
    let declaration_by_name = declarations
        .iter()
        .flat_map(|decl| decl.names.iter().map(|name| (name.clone(), decl.ordinal)))
        .collect::<BTreeMap<_, _>>();
    ChunkAstAnalysis {
        runtime_import_facts: RuntimeImportFacts { imports },
        declarations,
        declaration_by_name,
        destructure_siblings,
        pre_existing_entry_exports,
    }
}

/// For each top-level `var/let/const` declarator whose pattern binds
/// more than one name (i.e. a destructure like `const { x, y } = obj`
/// or `const [a, b] = arr`), record a sibling set mapping every name
/// in the pattern to the set of all names from that pattern.
/// Single-name declarators add nothing.
fn record_destructure_sibling_groups(
    item: &ModuleItem,
    out: &mut BTreeMap<String, BTreeSet<String>>,
) {
    let decl = match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => decl,
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => &export_decl.decl,
        _ => return,
    };
    let Decl::Var(var) = decl else {
        return;
    };
    for declarator in &var.decls {
        let names = binding_names(&declarator.name);
        if names.len() < 2 {
            continue;
        }
        let group: BTreeSet<String> = names.iter().cloned().collect();
        for name in &names {
            out.entry(name.clone())
                .or_default()
                .extend(group.iter().cloned());
        }
    }
}

/// Pick up `export { foo, bar as baz }` (no `from`) — i.e. re-exports
/// of locally-declared bindings. `export … from …` is excluded
/// because those don't bind a local name in entry. `ExportDecl`
/// (e.g. `export const foo = …`) is already covered by
/// `top_level_declaration_names` returning `(names, exported = true)`.
fn record_pre_existing_named_exports(item: &ModuleItem, out: &mut BTreeSet<String>) {
    let ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) = item else {
        return;
    };
    if named.src.is_some() {
        return;
    }
    for specifier in &named.specifiers {
        let ExportSpecifier::Named(specifier) = specifier else {
            continue;
        };
        // The exported value is the local binding (`orig`); the
        // public name (`exported`) is irrelevant to the
        // emit-resolvability check, which keys off the local name.
        if let Some(local) = module_export_ident_name(&specifier.orig) {
            out.insert(local);
        }
    }
}

fn top_level_declaration_names(item: &ModuleItem) -> (Vec<String>, bool) {
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => (declaration_names(decl), false),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
            (declaration_names(&export_decl.decl), true)
        }
        _ => (Vec::new(), false),
    }
}

fn declaration_names(decl: &Decl) -> Vec<String> {
    match decl {
        Decl::Fn(function) => vec![function.ident.sym.to_string()],
        Decl::Class(class) => vec![class.ident.sym.to_string()],
        Decl::Var(var) => var
            .decls
            .iter()
            .flat_map(|decl| binding_names(&decl.name))
            .collect(),
        _ => Vec::new(),
    }
}

fn binding_names(pattern: &Pat) -> Vec<String> {
    match pattern {
        Pat::Ident(ident) => vec![ident.id.sym.to_string()],
        Pat::Rest(rest) => binding_names(&rest.arg),
        Pat::Assign(assign) => binding_names(&assign.left),
        Pat::Array(array) => array
            .elems
            .iter()
            .flatten()
            .flat_map(binding_names)
            .collect(),
        Pat::Object(object) => object
            .props
            .iter()
            .flat_map(|prop| match prop {
                ObjectPatProp::KeyValue(key_value) => binding_names(&key_value.value),
                ObjectPatProp::Assign(assign) => vec![assign.key.id.sym.to_string()],
                ObjectPatProp::Rest(rest) => binding_names(&rest.arg),
            })
            .collect(),
        _ => Vec::new(),
    }
}

#[derive(Default)]
struct RefCollector {
    names: BTreeSet<String>,
    shadowed_scopes: Vec<BTreeSet<String>>,
}

impl Visit for RefCollector {
    fn visit_ident(&mut self, node: &Ident) {
        let name = node.sym.as_ref();
        if !self.is_shadowed(name) {
            self.names.insert(name.to_string());
        }
    }

    fn visit_binding_ident(&mut self, _node: &BindingIdent) {}

    fn visit_import_decl(&mut self, _node: &ImportDecl) {}

    fn visit_function(&mut self, node: &Function) {
        let shadowed = node
            .params
            .iter()
            .flat_map(|param| binding_names(&param.pat))
            .collect::<BTreeSet<_>>();
        self.with_shadowed_scope(shadowed, |collector| node.visit_children_with(collector));
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        let shadowed = node
            .params
            .iter()
            .flat_map(binding_names)
            .collect::<BTreeSet<_>>();
        self.with_shadowed_scope(shadowed, |collector| node.visit_children_with(collector));
    }

    fn visit_member_expr(&mut self, node: &MemberExpr) {
        node.obj.visit_with(self);
        if let MemberProp::Computed(computed) = &node.prop {
            computed.expr.visit_with(self);
        }
    }

    fn visit_prop_name(&mut self, node: &PropName) {
        if let PropName::Computed(computed) = node {
            computed.expr.visit_with(self);
        }
    }

    fn visit_jsx_element_name(&mut self, _node: &JSXElementName) {}

    fn visit_jsx_attr_name(&mut self, _node: &JSXAttrName) {}
}

impl RefCollector {
    fn is_shadowed(&self, name: &str) -> bool {
        self.shadowed_scopes
            .iter()
            .rev()
            .any(|scope| scope.contains(name))
    }

    fn with_shadowed_scope<F: FnOnce(&mut Self)>(&mut self, names: BTreeSet<String>, f: F) {
        self.shadowed_scopes.push(names);
        f(self);
        self.shadowed_scopes.pop();
    }
}

/// Drop `ImportSpecifier::Named` specifiers from a residual entry
/// body whose locals are unused after a logical-module move and
/// whose binding name is claimed by `Schedule.bindings`. If all
/// of an import directive's specifiers are dropped, the directive
/// is converted to a side-effect-only `import "<src>";` rather
/// than removed — the imported source-module's evaluation must
/// still be triggered from the residual entry, since some plans
/// (e.g. ImportSpecifier-only logical modules with no `Owned`
/// bindings) are not imported by the residual at runtime and so
/// cannot stand in for the source-module evaluation.
///
/// Default and namespace specifiers are kept as-is (a namespace
/// access can be hidden behind a computed property read;
/// defaults are similarly hard to ref-count safely).
/// Side-effect-only imports (`import "./mod.js"` with no
/// specifiers) pass through unchanged — they had no specifiers
/// to begin with.
fn trim_dead_named_specifiers(
    body: &mut [ModuleItem],
    bindings: &HashMap<BindingName, BindingKind>,
) {
    let mut collector = RefCollector::default();
    for item in body.iter() {
        item.visit_with(&mut collector);
    }
    let refs = collector.names;
    for item in body.iter_mut() {
        let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item else {
            continue;
        };
        // Side-effect-only imports never had specifiers; leave
        // them alone (they exist to evaluate the imported module).
        if import.specifiers.is_empty() {
            continue;
        }
        import.specifiers.retain(|spec| match spec {
            ImportSpecifier::Default(_) | ImportSpecifier::Namespace(_) => true,
            ImportSpecifier::Named(named) => {
                let local = named.local.sym.as_ref();
                let claimed = bindings.contains_key(local);
                let unused = !refs.contains(local);
                !(claimed && unused)
            }
        });
        // The directive's `specifiers: vec![]` shape is itself a
        // side-effect-only import — `import "./mod.js";`. Keeping
        // it preserves the source-module evaluation that the
        // original entry depended on, regardless of whether any
        // moved logical module is loaded by the residual.
    }
}

fn reject_duplicate_export_names(
    operation: &str,
    id: &str,
    members: &[MemberRequest],
) -> Result<()> {
    let mut seen = BTreeSet::new();
    let mut duplicates = BTreeSet::new();
    for member in members {
        if !seen.insert(member.export_name.clone()) {
            duplicates.insert(member.export_name.clone());
        }
    }
    if !duplicates.is_empty() {
        bail!(
            "{operation} {id} has duplicate exported logical names: {}",
            duplicates.into_iter().collect::<Vec<_>>().join(", ")
        );
    }
    Ok(())
}

fn reject_duplicate_member_bindings(
    operation: &str,
    id: &str,
    members: &[MemberRequest],
) -> Result<()> {
    let mut seen = BTreeSet::new();
    let mut duplicates = BTreeSet::new();
    for member in members {
        if !seen.insert(member.binding.clone()) {
            duplicates.insert(member.binding.clone());
        }
    }
    if !duplicates.is_empty() {
        bail!(
            "{operation} {id} has duplicate source bindings: {}",
            duplicates.into_iter().collect::<Vec<_>>().join(", ")
        );
    }
    Ok(())
}

fn collect_module_body_facts(body: &[ModuleItem]) -> ModuleBodyFacts {
    let mut facts = ModuleBodyFacts::default();
    let mut ref_collector = RefCollector::default();
    for item in body {
        item.visit_with(&mut ref_collector);
        facts
            .provided_locals
            .extend(top_level_declaration_names(item).0);
        if let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item {
            for specifier in &import.specifiers {
                match specifier {
                    ImportSpecifier::Named(named) => {
                        facts.imported_locals.insert(named.local.sym.to_string());
                        facts.provided_locals.insert(named.local.sym.to_string());
                    }
                    ImportSpecifier::Default(default) => {
                        facts.imported_locals.insert(default.local.sym.to_string());
                        facts.provided_locals.insert(default.local.sym.to_string());
                    }
                    ImportSpecifier::Namespace(namespace) => {
                        facts
                            .imported_locals
                            .insert(namespace.local.sym.to_string());
                        facts
                            .provided_locals
                            .insert(namespace.local.sym.to_string());
                    }
                }
            }
        }
    }
    facts.referenced_idents = ref_collector.names;
    facts
}

fn record_runtime_imports(item: &ModuleItem, imports: &mut BTreeMap<String, RuntimeImportInfo>) {
    let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item else {
        return;
    };
    let src = str_value(&import.src);
    for specifier in &import.specifiers {
        match specifier {
            ImportSpecifier::Named(named) => {
                let local = named.local.sym.to_string();
                let imported = match &named.imported {
                    Some(ModuleExportName::Ident(ident)) => ident.sym.to_string(),
                    Some(ModuleExportName::Str(s)) => str_value(s),
                    None => named.local.sym.to_string(),
                };
                imports.insert(
                    local,
                    RuntimeImportInfo {
                        kind: RuntimeImportKind::Named { imported },
                        src: src.clone(),
                    },
                );
            }
            ImportSpecifier::Default(default) => {
                imports.insert(
                    default.local.sym.to_string(),
                    RuntimeImportInfo {
                        kind: RuntimeImportKind::Default,
                        src: src.clone(),
                    },
                );
            }
            ImportSpecifier::Namespace(namespace) => {
                imports.insert(
                    namespace.local.sym.to_string(),
                    RuntimeImportInfo {
                        kind: RuntimeImportKind::Namespace,
                        src: src.clone(),
                    },
                );
            }
        }
    }
}

fn collect_imported_reexports_by_module(
    schedule: &Schedule,
    module_count: usize,
) -> Vec<Vec<ImportedReexport>> {
    let mut by_module: Vec<Vec<ImportedReexport>> = (0..module_count).map(|_| Vec::new()).collect();
    // Stable iteration order on `schedule.bindings` (HashMap): the
    // recorded sequence determines the emit order of
    // `import { ... }` statements per module body and we want that
    // source-level shape pinned.
    let mut sorted_bindings: Vec<(&BindingName, &BindingKind)> = schedule.bindings.iter().collect();
    sorted_bindings.sort_by(|a, b| a.0.cmp(b.0));
    for (local, kind) in sorted_bindings {
        let BindingKind::Imported {
            imported_name,
            imported_from,
            re_exporter,
            public_name,
        } = kind
        else {
            continue;
        };
        let ModuleId::Logical(LogicalModuleIndex(index)) = re_exporter else {
            continue;
        };
        let Some(reexports) = by_module.get_mut(*index) else {
            continue;
        };
        reexports.push(ImportedReexport {
            local: local.clone(),
            imported_name: imported_name.clone(),
            imported_from: imported_from.clone(),
            public_name: public_name.clone(),
        });
    }
    by_module
}

fn plan_module_reference_needs<'a>(
    module_index: usize,
    body_facts: &ModuleBodyFacts,
    schedule: &Schedule,
    declaration_by_name: &BTreeMap<String, usize>,
    binding_assignment: &BTreeMap<String, usize>,
    entry_exports_by_original_local: &BTreeMap<String, String>,
    runtime_import_facts: &'a RuntimeImportFacts,
) -> ModuleReferenceNeeds<'a> {
    let mut needs = ModuleReferenceNeeds::default();
    for name in &body_facts.referenced_idents {
        if let Some(ModuleId::Logical(LogicalModuleIndex(provider_index))) = schedule.owner_of(name)
        {
            if provider_index != module_index
                && let Some(provider) = schedule.logical_module(LogicalModuleIndex(provider_index))
                && let Some(exported_name) = provider.rename_map.get(name)
            {
                needs
                    .cross_module_imports_by_provider
                    .entry(provider_index)
                    .or_default()
                    .insert(name.clone(), exported_name.clone());
            }
            continue;
        }

        if !body_facts.provided_locals.contains(name)
            && !binding_assignment.contains_key(name)
            && declaration_by_name.contains_key(name)
        {
            if let Some(exported_name) = entry_exports_by_original_local.get(name) {
                needs
                    .residual_entry_imports
                    .insert(name.clone(), exported_name.clone());
            } else {
                needs.missing_residual_exports.insert(name.clone());
            }
            continue;
        }

        if body_facts.imported_locals.contains(name) {
            continue;
        }
        if let Some(info) = runtime_import_facts.imports.get(name) {
            needs.runtime_reimports.insert(name.clone(), info);
        }
    }
    needs
}

fn cross_module_imports_for_plan(
    from_file: &str,
    mut imports_by_provider: BTreeMap<usize, BTreeMap<String, String>>,
    schedule: &Schedule,
    occupied: &mut BTreeSet<String>,
    renames: &mut BTreeMap<String, String>,
) -> Vec<ModuleItem> {
    // Sort providers by their position in the schedule's
    // `linker_order` (a topological linearization of `I ∪ S`).
    // ECMA-262's depth-first link traversal visits each module's
    // `import` directives in source order, and the deepest leaf
    // reached first evaluates first. Putting the earliest-in-`L`
    // provider at the top of the import list steers the traversal
    // toward an `I ∪ S`-respecting evaluation order. See DESIGN.md
    // "Lemma 2".
    let mut providers: Vec<usize> = imports_by_provider.keys().copied().collect();
    providers.sort_by_key(|&idx| {
        schedule
            .linker_position(ModuleId::Logical(LogicalModuleIndex(idx)))
            .unwrap_or(usize::MAX)
    });
    providers
        .into_iter()
        .filter_map(|provider_index| {
            let bindings = imports_by_provider.remove(&provider_index)?;
            schedule
                .logical_module(LogicalModuleIndex(provider_index))
                .map(|provider| {
                    let resolved = disambiguate_import_locals(&bindings, occupied, renames);
                    import_decl_for_plan(from_file, &provider.target_file, &resolved)
                })
        })
        .collect()
}

fn residual_entry_imports_for_moved_body(
    module_id: &str,
    entry_file: &str,
    from_file: &str,
    imports: BTreeMap<String, String>,
    missing_exports: BTreeSet<String>,
    occupied: &mut BTreeSet<String>,
    renames: &mut BTreeMap<String, String>,
) -> Result<Vec<ModuleItem>> {
    if !missing_exports.is_empty() {
        bail!(
            "materialize_logical_modules: moved module {module_id} references residual entry binding(s) {} that are not exported by entry; refusing to emit free references. Keep those bindings with the moved module, expose them from entry, or use an explicit residual module.",
            missing_exports.into_iter().collect::<Vec<_>>().join(", "),
        );
    }
    if imports.is_empty() {
        return Ok(Vec::new());
    }
    let resolved = disambiguate_import_locals(&imports, occupied, renames);
    Ok(vec![import_decl_for_plan(from_file, entry_file, &resolved)])
}

fn collect_entry_exports_by_original_local(
    entry_body: &[ModuleItem],
    entry_renames: &BTreeMap<String, String>,
) -> BTreeMap<String, String> {
    let final_to_original = entry_renames
        .iter()
        .map(|(original, final_name)| (final_name.clone(), original.clone()))
        .collect::<BTreeMap<_, _>>();
    let mut exports = BTreeMap::<String, String>::new();
    for item in entry_body {
        match item {
            ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) if named.src.is_none() => {
                for specifier in &named.specifiers {
                    let ExportSpecifier::Named(specifier) = specifier else {
                        continue;
                    };
                    let Some(final_local) = module_export_ident_name(&specifier.orig) else {
                        continue;
                    };
                    let Some(exported_name) =
                        named_export_public_ident_name(&specifier.exported, &final_local)
                    else {
                        continue;
                    };
                    let original = final_to_original
                        .get(&final_local)
                        .cloned()
                        .unwrap_or(final_local);
                    exports.entry(original).or_insert(exported_name);
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
                for final_local in declaration_names(&export_decl.decl) {
                    let original = final_to_original
                        .get(&final_local)
                        .cloned()
                        .unwrap_or_else(|| final_local.clone());
                    exports.entry(original).or_insert(final_local);
                }
            }
            _ => {}
        }
    }
    exports
}

fn module_export_ident_name(name: &ModuleExportName) -> Option<String> {
    match name {
        ModuleExportName::Ident(ident) => Some(ident.sym.to_string()),
        ModuleExportName::Str(_) => None,
    }
}

fn named_export_public_ident_name(
    exported: &Option<ModuleExportName>,
    fallback: &str,
) -> Option<String> {
    match exported {
        Some(ModuleExportName::Ident(ident)) => Some(ident.sym.to_string()),
        Some(ModuleExportName::Str(_)) => None,
        None => Some(fallback.to_string()),
    }
}

fn final_module_exports(
    exports: &BTreeMap<String, String>,
    local_renames: &BTreeMap<String, String>,
) -> BTreeMap<String, String> {
    exports
        .iter()
        .map(|(local, exported)| {
            (
                local_renames
                    .get(local)
                    .cloned()
                    .unwrap_or_else(|| local.clone()),
                exported.clone(),
            )
        })
        .collect()
}

fn naturalize_module_body(body: &mut [ModuleItem], plan: &ModulePlan) -> BTreeMap<String, String> {
    let mut renames = BTreeMap::<String, String>::new();
    // Stable iteration over `plan.bindings` (a HashMap) so the order
    // renames land in `renames` — and thus the rename-precedence the
    // visitor applies when two locals compete for the same target —
    // doesn't vary by hash seed.
    let mut sorted_bindings: Vec<(&String, &String)> = plan.bindings.iter().collect();
    sorted_bindings.sort_by(|a, b| a.0.cmp(b.0));
    for (local, exported) in sorted_bindings {
        if local != exported && is_identifier_like(exported) {
            renames.insert(local.clone(), exported.clone());
        }
    }
    let mut heuristic = BTreeMap::<String, String>::new();
    for item in body.iter() {
        collect_naturalization_renames_from_item(item, &mut heuristic);
    }
    let renames = drop_target_collisions(renames, heuristic);
    if renames.is_empty() {
        for item in body.iter_mut() {
            item.visit_mut_with(&mut ShorthandNaturalizer);
        }
    } else {
        let mut naturalizer = RenameAndShorthandNaturalizer { renames: &renames };
        for item in body.iter_mut() {
            item.visit_mut_with(&mut naturalizer);
        }
    }
    renames
}

/// Merge `heuristic` into `plan_driven`, dropping any heuristic mapping
/// whose target is either already claimed by `plan_driven` or shared with
/// another heuristic source. Two sources renamed onto the same target
/// would collapse distinct bindings into a duplicate decl as soon as both
/// happen to live in the same scope.
fn drop_target_collisions(
    mut plan_driven: BTreeMap<String, String>,
    heuristic: BTreeMap<String, String>,
) -> BTreeMap<String, String> {
    // Only effective heuristic mappings (locals not already in plan_driven)
    // contribute to the collision count. Counting skipped entries inflates
    // counts[target] and can drop unrelated heuristic mappings that have only
    // one effective claimant.
    let mut counts = BTreeMap::<String, usize>::new();
    for target in plan_driven.values() {
        *counts.entry(target.clone()).or_default() += 1;
    }
    for (local, target) in &heuristic {
        if plan_driven.contains_key(local) {
            continue;
        }
        *counts.entry(target.clone()).or_default() += 1;
    }
    for (local, target) in heuristic {
        if plan_driven.contains_key(&local) {
            continue;
        }
        if counts.get(&target).copied().unwrap_or(0) > 1 {
            continue;
        }
        plan_driven.insert(local, target);
    }
    plan_driven
}

fn collect_naturalization_renames_from_item(
    item: &ModuleItem,
    renames: &mut BTreeMap<String, String>,
) {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Fn(function))) => {
            collect_naturalization_renames_from_function(&function.function, renames);
        }
        ModuleItem::Stmt(Stmt::Decl(Decl::Class(class))) => {
            collect_naturalization_renames_from_class(&class.class, renames);
        }
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => {
            for declarator in &var.decls {
                if let Some(init) = declarator.init.as_ref() {
                    collect_naturalization_renames_from_expr(init, renames);
                }
            }
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => match &export_decl.decl {
            Decl::Fn(function) => {
                collect_naturalization_renames_from_function(&function.function, renames);
            }
            Decl::Class(class) => {
                collect_naturalization_renames_from_class(&class.class, renames);
            }
            Decl::Var(var) => {
                for declarator in &var.decls {
                    if let Some(init) = declarator.init.as_ref() {
                        collect_naturalization_renames_from_expr(init, renames);
                    }
                }
            }
            _ => {}
        },
        _ => {}
    }
}

fn collect_naturalization_renames_from_expr(expr: &Expr, renames: &mut BTreeMap<String, String>) {
    match expr {
        Expr::Fn(function) => {
            collect_naturalization_renames_from_function(&function.function, renames)
        }
        Expr::Arrow(arrow) => {
            for param in &arrow.params {
                collect_naturalization_renames_from_pattern(param, renames);
            }
        }
        Expr::Class(class) => collect_naturalization_renames_from_class(&class.class, renames),
        _ => {}
    }
}

fn collect_naturalization_renames_from_function(
    function: &Function,
    renames: &mut BTreeMap<String, String>,
) {
    for param in &function.params {
        collect_naturalization_renames_from_pattern(&param.pat, renames);
    }
    let Some(body) = function.body.as_ref() else {
        return;
    };
    collect_return_object_alias_renames(&body.stmts, renames);
}

fn collect_naturalization_renames_from_class(
    class: &Class,
    renames: &mut BTreeMap<String, String>,
) {
    for member in &class.body {
        let ClassMember::Constructor(constructor) = member else {
            continue;
        };
        let mut param_names = BTreeSet::new();
        for param in &constructor.params {
            if let ParamOrTsParamProp::Param(param) = param
                && let Pat::Ident(ident) = &param.pat
            {
                param_names.insert(ident.id.sym.to_string());
            }
        }
        let Some(body) = constructor.body.as_ref() else {
            continue;
        };
        for statement in &body.stmts {
            collect_constructor_assignment_renames(statement, &param_names, renames);
        }
    }
}

fn collect_naturalization_renames_from_pattern(pat: &Pat, renames: &mut BTreeMap<String, String>) {
    match pat {
        Pat::Object(object) => {
            for prop in &object.props {
                match prop {
                    ObjectPatProp::KeyValue(key_value) => {
                        if let PropName::Ident(key) = &key_value.key
                            && let Pat::Ident(value) = &*key_value.value
                        {
                            let from = value.id.sym.to_string();
                            let to = key.sym.to_string();
                            if from != to && is_identifier_like(&to) {
                                renames.insert(from, to);
                            }
                        }
                    }
                    ObjectPatProp::Assign(_) => {}
                    ObjectPatProp::Rest(rest) => {
                        collect_naturalization_renames_from_pattern(&rest.arg, renames);
                    }
                }
            }
        }
        Pat::Array(array) => {
            for elem in array.elems.iter().flatten() {
                collect_naturalization_renames_from_pattern(elem, renames);
            }
        }
        Pat::Assign(assign) => collect_naturalization_renames_from_pattern(&assign.left, renames),
        Pat::Rest(rest) => collect_naturalization_renames_from_pattern(&rest.arg, renames),
        _ => {}
    }
}

fn collect_return_object_alias_renames(stmts: &[Stmt], renames: &mut BTreeMap<String, String>) {
    for stmt in stmts {
        match stmt {
            Stmt::Return(return_stmt) => {
                if let Some(expr) = &return_stmt.arg
                    && let Expr::Object(object) = &**expr
                {
                    for prop in &object.props {
                        if let PropOrSpread::Prop(prop) = prop
                            && let Prop::KeyValue(key_value) = &**prop
                            && let PropName::Ident(key) = &key_value.key
                            && let Expr::Ident(value) = &*key_value.value
                        {
                            let from = value.sym.to_string();
                            let to = key.sym.to_string();
                            if from != to && is_identifier_like(&to) {
                                renames.insert(from, to);
                            }
                        }
                    }
                }
            }
            Stmt::Block(block) => collect_return_object_alias_renames(&block.stmts, renames),
            _ => {}
        }
    }
}

fn collect_constructor_assignment_renames(
    stmt: &Stmt,
    param_names: &BTreeSet<String>,
    renames: &mut BTreeMap<String, String>,
) {
    let Stmt::Expr(expr_stmt) = stmt else {
        return;
    };
    let Expr::Assign(assign) = &*expr_stmt.expr else {
        return;
    };
    if assign.op != AssignOp::Assign {
        return;
    }
    let Some(target_name) = this_property_name(&assign.left) else {
        return;
    };
    let Expr::Ident(value) = &*assign.right else {
        return;
    };
    let from = value.sym.to_string();
    if param_names.contains(&from) && from != target_name && is_identifier_like(&target_name) {
        renames.insert(from, target_name);
    }
}

fn this_property_name(target: &AssignTarget) -> Option<String> {
    let AssignTarget::Simple(SimpleAssignTarget::Member(member)) = target else {
        return None;
    };
    if !matches!(&*member.obj, Expr::This(_)) {
        return None;
    }
    match &member.prop {
        MemberProp::Ident(ident) => Some(ident.sym.to_string()),
        MemberProp::Computed(computed) => match &*computed.expr {
            Expr::Lit(Lit::Str(value)) if is_identifier_like(&str_value(value)) => {
                Some(str_value(value))
            }
            _ => None,
        },
        _ => None,
    }
}

struct IdentifierRenamer<'a> {
    renames: &'a BTreeMap<String, String>,
}

impl VisitMut for IdentifierRenamer<'_> {
    fn visit_mut_ident(&mut self, ident: &mut Ident) {
        if let Some(to) = self.renames.get(ident.sym.as_ref()) {
            ident.sym = to.clone().into();
        }
    }

    fn visit_mut_import_named_specifier(&mut self, spec: &mut ImportNamedSpecifier) {
        let original_local = spec.local.sym.clone();
        let Some(to) = self.renames.get(original_local.as_ref()) else {
            return;
        };
        if spec.imported.is_none() {
            spec.imported = Some(ModuleExportName::Ident(Ident::new_no_ctxt(
                original_local,
                DUMMY_SP,
            )));
        }
        spec.local.sym = to.clone().into();
    }

    fn visit_mut_prop_name(&mut self, prop_name: &mut PropName) {
        if let PropName::Computed(computed) = prop_name {
            computed.visit_mut_children_with(self);
        }
    }

    fn visit_mut_member_prop(&mut self, member_prop: &mut MemberProp) {
        if let MemberProp::Computed(computed) = member_prop {
            computed.visit_mut_children_with(self);
        }
    }

    fn visit_mut_named_export(&mut self, named: &mut NamedExport) {
        // Re-export specifiers' orig field (`export { x } from "./mod"`) is
        // the imported name in the source module, not a local binding here,
        // so don't touch it. Without `from`, orig is a local binding —
        // recurse into specifiers so visit_mut_export_named_specifier can
        // narrow which fields to rewrite.
        if named.src.is_none() {
            named.specifiers.visit_mut_with(self);
        }
    }

    fn visit_mut_export_named_specifier(&mut self, spec: &mut ExportNamedSpecifier) {
        // The `exported` field is a public-API name, not a local binding,
        // so it must not be rewritten when a colliding local is renamed.
        spec.orig.visit_mut_with(self);
    }
}

struct RenameAndShorthandNaturalizer<'a> {
    renames: &'a BTreeMap<String, String>,
}

impl VisitMut for RenameAndShorthandNaturalizer<'_> {
    fn visit_mut_ident(&mut self, ident: &mut Ident) {
        if let Some(to) = self.renames.get(ident.sym.as_ref()) {
            ident.sym = to.clone().into();
        }
    }

    fn visit_mut_import_named_specifier(&mut self, spec: &mut ImportNamedSpecifier) {
        let original_local = spec.local.sym.clone();
        let Some(to) = self.renames.get(original_local.as_ref()) else {
            return;
        };
        if spec.imported.is_none() {
            spec.imported = Some(ModuleExportName::Ident(Ident::new_no_ctxt(
                original_local,
                DUMMY_SP,
            )));
        }
        spec.local.sym = to.clone().into();
    }

    fn visit_mut_prop_name(&mut self, prop_name: &mut PropName) {
        if let PropName::Computed(computed) = prop_name {
            computed.visit_mut_children_with(self);
        }
    }

    fn visit_mut_member_prop(&mut self, member_prop: &mut MemberProp) {
        if let MemberProp::Computed(computed) = member_prop {
            computed.visit_mut_children_with(self);
        }
    }

    fn visit_mut_named_export(&mut self, named: &mut NamedExport) {
        if named.src.is_none() {
            named.specifiers.visit_mut_with(self);
        }
    }

    fn visit_mut_export_named_specifier(&mut self, spec: &mut ExportNamedSpecifier) {
        spec.orig.visit_mut_with(self);
    }

    fn visit_mut_object_pat(&mut self, object: &mut ObjectPat) {
        object.visit_mut_children_with(self);
        naturalize_object_pattern_shorthand(object);
    }

    fn visit_mut_object_lit(&mut self, object: &mut ObjectLit) {
        object.visit_mut_children_with(self);
        naturalize_object_literal_shorthand(object);
    }
}

struct ShorthandNaturalizer;

impl VisitMut for ShorthandNaturalizer {
    fn visit_mut_object_pat(&mut self, object: &mut ObjectPat) {
        object.visit_mut_children_with(self);
        naturalize_object_pattern_shorthand(object);
    }

    fn visit_mut_object_lit(&mut self, object: &mut ObjectLit) {
        object.visit_mut_children_with(self);
        naturalize_object_literal_shorthand(object);
    }
}

fn naturalize_object_pattern_shorthand(object: &mut ObjectPat) {
    for prop in &mut object.props {
        if let ObjectPatProp::KeyValue(key_value) = prop
            && let PropName::Ident(key) = &key_value.key
            && let Pat::Ident(value) = &*key_value.value
            && key.sym == value.id.sym
        {
            *prop = ObjectPatProp::Assign(AssignPatProp {
                span: DUMMY_SP,
                key: value.clone(),
                value: None,
            });
        }
    }
}

fn naturalize_object_literal_shorthand(object: &mut ObjectLit) {
    for prop in &mut object.props {
        if let PropOrSpread::Prop(prop_box) = prop
            && let Prop::KeyValue(key_value) = &**prop_box
            && let PropName::Ident(key) = &key_value.key
            && let Expr::Ident(value) = &*key_value.value
            && key.sym == value.sym
        {
            *prop = PropOrSpread::Prop(Box::new(Prop::Shorthand(value.clone())));
        }
    }
}

fn rewrite_runtime_sources_for_target(body: &mut [ModuleItem], target_file: &str) {
    let target_dir = Path::new(target_file)
        .parent()
        .and_then(Path::to_str)
        .unwrap_or("")
        .replace('\\', "/");
    let mut rewriter = RuntimeSourceRewriter { target_dir };
    for item in body {
        item.visit_mut_with(&mut rewriter);
    }
}

struct RuntimeSourceRewriter {
    target_dir: String,
}

impl RuntimeSourceRewriter {
    fn rewrite(&self, source: &str) -> String {
        let original = normalize_module_path(source).unwrap_or_else(|_| source.to_string());
        // Original import sources in lowered module bodies are chunk-root-relative;
        // the lowered file lives at <target_dir>/<basename> within the chunk, so the
        // rewritten specifier walks up out of target_dir to chunk root.
        let mut rel = relative_module_path(&self.target_dir, &original);
        if !rel.starts_with('.') {
            rel = format!("./{rel}");
        }
        rel
    }
}

impl VisitMut for RuntimeSourceRewriter {
    fn visit_mut_call_expr(&mut self, call: &mut CallExpr) {
        call.visit_mut_children_with(self);
        if matches!(call.callee, Callee::Import(_))
            && let Some(first) = call.args.first_mut()
            && let Expr::Lit(Lit::Str(source)) = &mut *first.expr
        {
            set_str_value(source, self.rewrite(&str_value(source)));
        }
    }

    fn visit_mut_new_expr(&mut self, new_expr: &mut NewExpr) {
        new_expr.visit_mut_children_with(self);
        let Expr::Ident(callee) = &*new_expr.callee else {
            return;
        };
        if callee.sym != *"Worker" && callee.sym != *"SharedWorker" {
            return;
        }
        let Some(args) = new_expr.args.as_mut() else {
            return;
        };
        let Some(first) = args.first_mut() else {
            return;
        };
        let Expr::Lit(Lit::Str(source)) = &*first.expr else {
            return;
        };
        first.expr = Box::new(new_url_expr(&self.rewrite(&str_value(source))));
    }
}

fn new_url_expr(source: &str) -> Expr {
    Expr::New(NewExpr {
        span: DUMMY_SP,
        ctxt: SyntaxContext::empty(),
        callee: Box::new(Expr::Ident(Ident::new_no_ctxt("URL".into(), DUMMY_SP))),
        args: Some(vec![
            ExprOrSpread {
                spread: None,
                expr: Box::new(Expr::Lit(Lit::Str(Str {
                    span: DUMMY_SP,
                    value: source.into(),
                    raw: None,
                }))),
            },
            ExprOrSpread {
                spread: None,
                expr: Box::new(import_meta_url_expr()),
            },
        ]),
        type_args: None,
    })
}

fn import_meta_url_expr() -> Expr {
    Expr::Member(MemberExpr {
        span: DUMMY_SP,
        obj: Box::new(Expr::MetaProp(MetaPropExpr {
            span: DUMMY_SP,
            kind: MetaPropKind::ImportMeta,
        })),
        prop: MemberProp::Ident(IdentName::new("url".into(), DUMMY_SP)),
    })
}

fn is_identifier_like(name: &str) -> bool {
    let mut chars = name.chars();
    let Some(first) = chars.next() else {
        return false;
    };
    if !(first == '_' || first == '$' || first.is_ascii_alphabetic()) {
        return false;
    }
    chars.all(|ch| ch == '_' || ch == '$' || ch.is_ascii_alphanumeric())
}

fn target_file_for_request(target_dir: &str, target_path: &str) -> Result<String> {
    let normalized = normalize_module_path(target_path)?;
    let with_ext = if normalized.ends_with(".js") {
        normalized
    } else {
        format!("{normalized}.js")
    };
    Ok(join_module_path(&[target_dir, &with_ext]))
}

fn normalize_optional_relative_dir(value: &str) -> Result<String> {
    if value.is_empty() {
        return Ok(String::new());
    }
    normalize_module_path(value)
}

fn remaining_item_after_selection(
    item: &ModuleItem,
    binding_assignment: &BTreeMap<String, usize>,
    selected_by_module: &mut [Vec<ModuleItem>],
) -> Result<Vec<ModuleItem>> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => {
            split_var_decl(var, false, binding_assignment, selected_by_module)
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => match &export_decl.decl {
            Decl::Var(var) => split_var_decl(var, true, binding_assignment, selected_by_module),
            decl => {
                let names = declaration_names(decl);
                if let Some(module_index) = assigned_module_for_names(&names, binding_assignment) {
                    selected_by_module[module_index]
                        .push(ModuleItem::Stmt(Stmt::Decl(decl.clone())));
                    Ok(Vec::new())
                } else {
                    Ok(vec![item.clone()])
                }
            }
        },
        ModuleItem::Stmt(Stmt::Decl(decl)) => {
            let names = declaration_names(decl);
            if let Some(module_index) = assigned_module_for_names(&names, binding_assignment) {
                selected_by_module[module_index].push(item.clone());
                Ok(Vec::new())
            } else {
                Ok(vec![item.clone()])
            }
        }
        _ => Ok(vec![item.clone()]),
    }
}

fn split_var_decl(
    var: &VarDecl,
    was_exported: bool,
    binding_assignment: &BTreeMap<String, usize>,
    selected_by_module: &mut [Vec<ModuleItem>],
) -> Result<Vec<ModuleItem>> {
    let mut residual_decls = Vec::new();
    for declarator in &var.decls {
        let names = binding_names(&declarator.name);
        if let Some(module_index) = assigned_module_for_names(&names, binding_assignment) {
            let selected_var = VarDecl {
                span: var.span,
                ctxt: var.ctxt,
                kind: var.kind,
                declare: var.declare,
                decls: vec![declarator.clone()],
            };
            selected_by_module[module_index].push(ModuleItem::Stmt(Stmt::Decl(Decl::Var(
                Box::new(selected_var),
            ))));
        } else {
            residual_decls.push(declarator.clone());
        }
    }
    if residual_decls.is_empty() {
        return Ok(Vec::new());
    }
    let residual_var = VarDecl {
        span: var.span,
        ctxt: var.ctxt,
        kind: var.kind,
        declare: var.declare,
        decls: residual_decls,
    };
    if was_exported {
        Ok(vec![ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(
            ExportDecl {
                span: DUMMY_SP,
                decl: Decl::Var(Box::new(residual_var)),
            },
        ))])
    } else {
        Ok(vec![ModuleItem::Stmt(Stmt::Decl(Decl::Var(Box::new(
            residual_var,
        ))))])
    }
}

fn assigned_module_for_names(
    names: &[String],
    binding_assignment: &BTreeMap<String, usize>,
) -> Option<usize> {
    names
        .iter()
        .filter_map(|name| binding_assignment.get(name).copied())
        .next()
}

/// Names occupying the file-scope binding namespace of `body`.
///
/// Used to disambiguate consumer-side `import { exportedName as localName }`
/// emissions whose `localName` would collide with another binding in the
/// same scope (e.g. a surviving import or top-level declaration that
/// already uses the input-bundle name). `export { name }` re-exports without
/// `from` are references, not bindings, so they aren't tracked here; the
/// IdentifierRenamer pass that follows the disambiguation rewrites their
/// `orig` ident along with every other body reference.
fn collect_occupied_local_names(body: &[ModuleItem]) -> BTreeSet<String> {
    let mut occupied = BTreeSet::new();
    for item in body {
        match item {
            ModuleItem::ModuleDecl(ModuleDecl::Import(import)) => {
                for specifier in &import.specifiers {
                    match specifier {
                        ImportSpecifier::Named(named) => {
                            occupied.insert(named.local.sym.to_string());
                        }
                        ImportSpecifier::Default(default) => {
                            occupied.insert(default.local.sym.to_string());
                        }
                        ImportSpecifier::Namespace(namespace) => {
                            occupied.insert(namespace.local.sym.to_string());
                        }
                    }
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
                for name in declaration_names(&export_decl.decl) {
                    occupied.insert(name);
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(default_decl)) => {
                if let DefaultDecl::Class(class) = &default_decl.decl
                    && let Some(ident) = &class.ident
                {
                    occupied.insert(ident.sym.to_string());
                }
                if let DefaultDecl::Fn(function) = &default_decl.decl
                    && let Some(ident) = &function.ident
                {
                    occupied.insert(ident.sym.to_string());
                }
            }
            ModuleItem::Stmt(Stmt::Decl(decl)) => {
                for name in declaration_names(decl) {
                    occupied.insert(name);
                }
            }
            _ => {}
        }
    }
    occupied
}

/// Names bound anywhere under `body`. This is stricter than file-scope
/// occupancy: readable import locals must avoid nested bindings too, or the
/// follow-up body rewrite can accidentally capture references that were
/// supposed to resolve to the import.
fn collect_local_binding_names(body: &[ModuleItem]) -> BTreeSet<String> {
    struct Collector {
        names: BTreeSet<String>,
    }

    impl Visit for Collector {
        fn visit_binding_ident(&mut self, ident: &BindingIdent) {
            self.names.insert(ident.id.sym.to_string());
        }
    }

    let mut collector = Collector {
        names: BTreeSet::new(),
    };
    for item in body {
        item.visit_with(&mut collector);
    }
    collector.names
}

/// Map plan-side `original -> exported` to `actual_local -> exported`.
///
/// When a spec gives a binding a readable exported name, prefer that
/// readable name as the consumer-side local too. That keeps the final
/// emitted tree from retaining the input-bundle name merely as an import
/// alias. Collisions still mint a fresh local and get recorded in
/// `renames` so the entry body can be rewritten after emission.
fn disambiguate_import_locals(
    bindings: &BTreeMap<String, String>,
    occupied: &mut BTreeSet<String>,
    renames: &mut BTreeMap<String, String>,
) -> BTreeMap<String, String> {
    bindings
        .iter()
        .map(|(original, exported)| {
            let preferred = if exported != original {
                exported.as_str()
            } else {
                original.as_str()
            };
            let actual = if occupied.contains(preferred) {
                mint_fresh_local_name(preferred, occupied)
            } else {
                preferred.to_string()
            };
            occupied.insert(actual.clone());
            if actual != *original {
                renames.insert(original.clone(), actual.clone());
            }
            (actual, exported.clone())
        })
        .collect()
}

/// Pre-fill `exported` on `export { local }` re-export specifiers whose
/// `local` is about to be renamed, so the public export name survives.
fn preserve_export_specifier_names(item: &mut ModuleItem, renames: &BTreeMap<String, String>) {
    let ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) = item else {
        return;
    };
    for specifier in &mut named.specifiers {
        let ExportSpecifier::Named(spec) = specifier else {
            continue;
        };
        if spec.exported.is_some() {
            continue;
        }
        let ModuleExportName::Ident(orig) = &spec.orig else {
            continue;
        };
        if !renames.contains_key(&orig.sym.to_string()) {
            continue;
        }
        spec.exported = Some(spec.orig.clone());
    }
}

fn mint_fresh_local_name(base: &str, occupied: &BTreeSet<String>) -> String {
    let mut suffix = 1usize;
    loop {
        let candidate = format!("{base}${suffix}");
        if !occupied.contains(&candidate) {
            return candidate;
        }
        suffix += 1;
    }
}

/// Look up an `ImportSpecifier`-bound member's source-chunk import
/// statement and resolve it to `(imported_name, imported_from)` where
/// `imported_from` is the output-tree-rooted absolute path of the
/// import source (suitable for storing on `BindingKind::Imported`).
/// Per-destination relative paths are computed at emit time via
/// `relative_source(dest_target_file, imported_from)`.
fn resolve_imported_binding(
    source_import_cache: &mut ArtifactSourceImportResolutionCache<'_>,
    runtime_import_facts: &RuntimeImportFacts,
    source_chunk_id: &str,
    source_runtime_file: &str,
    source_local: &str,
    imported_from_by_src: &mut BTreeMap<String, String>,
) -> Result<(String, String)> {
    let Some(info) = runtime_import_facts.imports.get(source_local) else {
        bail!("no import specifier found for `{source_local}` in source chunk");
    };
    let RuntimeImportKind::Named { imported } = &info.kind else {
        bail!("no named import specifier found for `{source_local}` in source chunk");
    };
    let imported_from = if let Some(imported_from) = imported_from_by_src.get(&info.src) {
        imported_from.clone()
    } else {
        let imported_from = if let Some((_, _, path)) =
            source_import_cache.resolve(&info.src, source_chunk_id, source_runtime_file)?
        {
            path
        } else {
            // Source path doesn't reference a known chunk (e.g. a
            // synthetic e2e snapshot file with no entry in the artifact).
            // Resolve relative to the source chunk's directory in the
            // output tree (chunk_id includes the directory prefix; the
            // runtime file is chunk-relative).
            let chunk_runtime_abs = join_module_path(&[
                &module_path_dirname(source_chunk_id),
                &module_path_dirname(source_runtime_file),
            ]);
            join_module_path(&[&chunk_runtime_abs, &info.src])
        };
        imported_from_by_src.insert(info.src.clone(), imported_from.clone());
        imported_from
    };
    Ok((imported.clone(), imported_from))
}

/// Build re-imports for source-chunk ImportSpecifier-bound locals that
/// `body` (the moved code for this destination module) references but
/// no enclosing import or local decl provides. Each emitted import
/// uses a destination-relative path resolved through the artifact's
/// source-chunk index, so it stays correct after the rewriter (which
/// skips materialized files).
///
/// Bindings sharing the same rewritten source are consolidated into a
/// single `ImportDecl` (one statement with all specifiers) so the
/// emitter matches what an author would write — not one statement per
/// binding. Namespace specifiers (`import * as ns from "src"`) are
/// emitted as their own `ImportDecl` even when a same-source group
/// also has named/default specifiers, because ESM grammar forbids
/// mixing `NameSpaceImport` with `NamedImports` in a single
/// `ImportClause`. First-occurrence order is preserved both for the
/// source groups and for specifiers within each group.
fn source_chunk_imports_for_moved_body(
    source_import_cache: &mut ArtifactSourceImportResolutionCache<'_>,
    source_chunk_id: &str,
    source_runtime_file: &str,
    dest_target_file: &str,
    needed: BTreeMap<String, &RuntimeImportInfo>,
) -> Result<Vec<ModuleItem>> {
    let dest_dir = join_module_path(&[source_chunk_id, &module_path_dirname(dest_target_file)]);
    let mut groups: Vec<(String, Vec<ImportSpecifier>, Vec<ImportSpecifier>)> = Vec::new();
    let mut index_by_source: BTreeMap<String, usize> = BTreeMap::new();
    for (local, info) in needed {
        let rewritten_source = if let Some((target_chunk_id, target_entry_file, _path)) =
            source_import_cache.resolve(&info.src, source_chunk_id, source_runtime_file)?
        {
            let target_path = join_module_path(&[&target_chunk_id, &target_entry_file]);
            let mut rel = relative_module_path(&dest_dir, &target_path);
            if !rel.starts_with('.') {
                rel = format!("./{rel}");
            }
            rel
        } else {
            let depth = std::path::Path::new(dest_target_file)
                .parent()
                .map(|parent| parent.iter().count())
                .unwrap_or(0);
            format!("{}{}", "../".repeat(depth), info.src)
        };
        let specifier = runtime_reimport_specifier(&local, info);
        let group_index = *index_by_source
            .entry(rewritten_source.clone())
            .or_insert_with(|| {
                groups.push((rewritten_source.clone(), Vec::new(), Vec::new()));
                groups.len() - 1
            });
        let (_, named_or_default, namespace) = &mut groups[group_index];
        match specifier {
            ImportSpecifier::Namespace(_) => namespace.push(specifier),
            _ => named_or_default.push(specifier),
        }
    }
    let mut result = Vec::with_capacity(groups.len());
    for (src, mut named_or_default, mut namespace) in groups {
        // Emit namespace specifiers as their own ImportDecl each: ESM
        // forbids mixing them with NamedImports in one ImportClause,
        // and even multiple `import * as ns from "src"` for the same
        // source cannot share a statement (one ImportClause has at
        // most one NameSpaceImport).
        for ns_specifier in namespace.drain(..) {
            result.push(import_decl_module_item(vec![ns_specifier], &src));
        }
        if !named_or_default.is_empty() {
            // Sort default specifiers before named to satisfy ESM
            // grammar (`import D, { x } from "src"`, not the reverse).
            named_or_default.sort_by_key(|specifier| match specifier {
                ImportSpecifier::Default(_) => 0,
                _ => 1,
            });
            result.push(import_decl_module_item(named_or_default, &src));
        }
    }
    Ok(result)
}

fn import_decl_module_item(specifiers: Vec<ImportSpecifier>, src: &str) -> ModuleItem {
    ModuleItem::ModuleDecl(ModuleDecl::Import(ImportDecl {
        span: DUMMY_SP,
        specifiers,
        src: Box::new(Str {
            span: DUMMY_SP,
            value: src.into(),
            raw: None,
        }),
        type_only: false,
        with: None,
        phase: ImportPhase::Evaluation,
    }))
}

#[derive(Debug)]
struct RuntimeImportInfo {
    kind: RuntimeImportKind,
    src: String,
}

#[derive(Debug)]
enum RuntimeImportKind {
    Named { imported: String },
    Default,
    Namespace,
}

fn runtime_reimport_specifier(local: &str, info: &RuntimeImportInfo) -> ImportSpecifier {
    match &info.kind {
        RuntimeImportKind::Named { imported } => ImportSpecifier::Named(ImportNamedSpecifier {
            span: DUMMY_SP,
            local: Ident::new_no_ctxt(local.into(), DUMMY_SP),
            imported: if imported == local {
                None
            } else {
                Some(ModuleExportName::Ident(Ident::new_no_ctxt(
                    imported.clone().into(),
                    DUMMY_SP,
                )))
            },
            is_type_only: false,
        }),
        RuntimeImportKind::Default => ImportSpecifier::Default(ImportDefaultSpecifier {
            span: DUMMY_SP,
            local: Ident::new_no_ctxt(local.into(), DUMMY_SP),
        }),
        RuntimeImportKind::Namespace => ImportSpecifier::Namespace(ImportStarAsSpecifier {
            span: DUMMY_SP,
            local: Ident::new_no_ctxt(local.into(), DUMMY_SP),
        }),
    }
}

/// Build a single Named specifier (`{ <imported> as <local> }`, or just
/// `{ <local> }` when local == imported) for an ImportSpecifier-bound
/// reexport. Callers group same-source specifiers and wrap the list in
/// one `ImportDecl` via [`import_decl_module_item`].
fn imported_binding_named_specifier(local: &str, imported: &str) -> ImportSpecifier {
    ImportSpecifier::Named(ImportNamedSpecifier {
        span: DUMMY_SP,
        local: Ident::new_no_ctxt(local.into(), DUMMY_SP),
        imported: if local == imported {
            None
        } else {
            Some(ModuleExportName::Ident(Ident::new_no_ctxt(
                imported.into(),
                DUMMY_SP,
            )))
        },
        is_type_only: false,
    })
}

fn import_decl_for_plan(
    entry_file: &str,
    target_file: &str,
    bindings: &BTreeMap<String, String>,
) -> ModuleItem {
    let source = relative_source(entry_file, target_file);
    ModuleItem::ModuleDecl(ModuleDecl::Import(ImportDecl {
        span: DUMMY_SP,
        specifiers: bindings
            .iter()
            .map(|(local, exported)| {
                ImportSpecifier::Named(ImportNamedSpecifier {
                    span: DUMMY_SP,
                    local: Ident::new_no_ctxt(local.clone().into(), DUMMY_SP),
                    imported: if local == exported {
                        None
                    } else {
                        Some(ModuleExportName::Ident(Ident::new_no_ctxt(
                            exported.clone().into(),
                            DUMMY_SP,
                        )))
                    },
                    is_type_only: false,
                })
            })
            .collect(),
        src: Box::new(Str {
            span: DUMMY_SP,
            value: source.into(),
            raw: None,
        }),
        type_only: false,
        with: None,
        phase: ImportPhase::Evaluation,
    }))
}

fn relative_source(from_file: &str, target_file: &str) -> String {
    let from_dir = std::path::Path::new(from_file)
        .parent()
        .and_then(|parent| parent.to_str())
        .unwrap_or("")
        .replace('\\', "/");
    let mut rel = relative_module_path(&from_dir, target_file);
    if !rel.starts_with('.') {
        rel = format!("./{rel}");
    }
    rel
}

fn export_named_for_bindings(bindings: &BTreeMap<String, String>) -> ModuleItem {
    ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(NamedExport {
        span: DUMMY_SP,
        specifiers: bindings
            .iter()
            .map(|(local, exported)| {
                ExportSpecifier::Named(ExportNamedSpecifier {
                    span: DUMMY_SP,
                    orig: ModuleExportName::Ident(Ident::new_no_ctxt(
                        local.clone().into(),
                        DUMMY_SP,
                    )),
                    exported: if local == exported {
                        None
                    } else {
                        Some(ModuleExportName::Ident(Ident::new_no_ctxt(
                            exported.clone().into(),
                            DUMMY_SP,
                        )))
                    },
                    is_type_only: false,
                })
            })
            .collect(),
        src: None,
        type_only: false,
        with: None,
    }))
}

fn entry_exports_for_moved_bindings(
    declarations: &[TopLevelDecl],
    binding_assignment: &BTreeMap<String, usize>,
    entry_renames: &BTreeMap<String, String>,
) -> Vec<ModuleItem> {
    let mut exports = BTreeMap::<String, String>::new();
    for decl in declarations.iter().filter(|decl| decl.exported) {
        for name in &decl.names {
            if binding_assignment.contains_key(name) {
                let final_local = entry_renames
                    .get(name)
                    .cloned()
                    .unwrap_or_else(|| name.clone());
                exports.insert(final_local, name.clone());
            }
        }
    }
    if exports.is_empty() {
        Vec::new()
    } else {
        vec![export_named_for_bindings(&exports)]
    }
}

fn prune_artifact_to_chunk_ids(artifact: &mut JsPipelineArtifact, selected: &[String]) {
    let selected_names: BTreeSet<String> = selected.iter().cloned().collect();
    let selected_ids: std::collections::HashSet<ChunkId> = selected
        .iter()
        .filter_map(|name| artifact.chunk_table.get(name))
        .collect();
    artifact.retain_chunks(|chunk_id| selected_ids.contains(&chunk_id));
    artifact
        .root_manifest
        .chunks
        .retain(|chunk| selected_names.contains(&chunk.chunk_id));
    artifact.root_manifest.counts.chunks = artifact.root_manifest.chunks.len();
}

fn update_root_manifest(
    artifact: &mut JsPipelineArtifact,
    reports: &[LogicalChunkReport],
    applied: &[SelectedModuleLowering],
) {
    artifact.root_manifest.counts.selected_module_lowerings = Some(applied.len());
    artifact.root_manifest.logical_modules = Some(RootLogicalModulesSummary {
        module_count: reports.iter().map(|r| r.counts.final_modules).sum(),
    });
    artifact.root_manifest.selected_module_lowerings = Some(applied.to_vec());
}

fn write_chunk_report_json<T: Serialize>(
    report_out_dir: &Path,
    chunk_id: &str,
    filename: &str,
    value: &T,
) -> Result<()> {
    let path = report_out_dir
        .join(chunk_id.split('/').collect::<PathBuf>())
        .join(filename);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    if filename == "owner_graph.json" {
        // This side output is large enough on real app chunks that pretty
        // printing meaningfully affects local and remote test artifact size.
        // Keep small human-first reports pretty; keep the graph jq-first.
        let mut output = BufWriter::new(fs::File::create(path)?);
        serde_json::to_writer(&mut output, value)?;
        writeln!(output)?;
    } else {
        let body = serde_json::to_string_pretty(value)?;
        fs::write(path, body + "\n")?;
    }
    Ok(())
}

fn prepare_output_dir(out_dir: &Path, force: bool) -> Result<()> {
    if out_dir.exists() {
        if !out_dir.is_dir() {
            bail!(
                "Output path exists and is not a directory: {}",
                out_dir.display()
            );
        }
        if fs::read_dir(out_dir)?.next().is_some() && !force {
            bail!(
                "Output directory is not empty: {}. Pass --force to replace it.",
                out_dir.display()
            );
        }
        if force {
            fs::remove_dir_all(out_dir)?;
        }
    }
    fs::create_dir_all(out_dir)?;
    Ok(())
}

/// Per-cause guidance for the atomic-unit-conflict bail message —
/// gives the spec author vocabulary to search for (`cycle`,
/// `side-effect`, `mutable`, `assignment`, `cross-destination`).
fn render_atomic_unit_cause_guidance(conflicts: &[AtomicUnitConflictReport]) -> String {
    let mut causes: Vec<DepKind> = conflicts
        .iter()
        .flat_map(|c| c.causes.iter().copied())
        .collect::<HashSet<_>>()
        .into_iter()
        .collect();
    causes.sort();
    let mut out = String::new();
    for cause in &causes {
        out.push_str(match cause {
            DepKind::EagerUse => {
                "EagerUse cycle: a top-level statement reads a binding at-init; \
                 splitting reader and declarer across modules forms an evaluation-order cycle. "
            }
            DepKind::EagerRebind | DepKind::LazyRebind => {
                "Rebind: a function or top-level statement performs an assignment \
                 to a mutable binding owned by a different module — the resulting ESM \
                 import would be read-only, so this cross-destination assignment is invalid. \
                 The assigner and the binding declarer must materialize together. "
            }
            DepKind::Sequenced => {
                "Sequenced side-effect chain: two top-level side-effect statements are \
                 forced into a fixed source order; splitting them across modules \
                 inverts the run order. "
            }
            DepKind::LazyUse => continue,
        });
    }
    out
}
