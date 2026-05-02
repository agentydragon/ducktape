use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Instant;

use anyhow::{Context, Result, bail};
use serde::Serialize;
use serde_json::{Value, json};
use swc_common::{DUMMY_SP, SyntaxContext};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitMut, VisitMutWith, VisitWith};

use artifact::{
    ArtifactCounts, ArtifactManifest, ChunkFileRecord, ChunkLogicalModulesSummary, ChunkMetadata,
    FileMetadata, JsChunk, JsFile, JsPipelineArtifact, RootLogicalModulesSummary,
    SelectedModuleLowering, get_chunk_entry_path, normalize_relative_path, posix_join,
    posix_relative,
};
use js_ast::{ParsedJsModule, set_str_value, str_value};
use write_tree::resolve_workspace_path;

const LOWERING_FILE_PRAGMA: &str =
    "// @ducktape-generated kind=lowerer-helper stage=selected_module_lowering ignore=detectors";
const LOWERING_GENERATOR_HEADER: &str = "// @ducktape-generator selected_module_lowering";

/// Per-stage manifest returned by `materialize_logical_modules`.
///
/// Pipeline currently consumes only `kind` for stage logging; the
/// remaining fields are written to disk when a spec sets
/// `report_summary_path`. Keep them serializable so that callers using
/// that argument get a typed payload.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LogicalModuleManifest {
    pub chunk_count: usize,
    pub chunks: Vec<LogicalChunkReport>,
    pub counts: LogicalModuleCounts,
    pub duration_ms: f64,
    pub kind: &'static str,
    pub report_out_dir: Option<String>,
    pub schema_version: u32,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LogicalModuleCounts {
    pub applied: usize,
    pub final_modules: usize,
    pub explicit_logical_modules: usize,
    pub residual_logical_modules: usize,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LogicalChunkReport {
    pub chunk_id: String,
    pub counts: LogicalChunkCounts,
    pub final_module_contents: Vec<FinalModuleContent>,
    pub requested_logical_modules: Vec<RequestedLogicalModule>,
    pub timings_ms: BTreeMap<String, f64>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LogicalChunkCounts {
    pub applied: usize,
    pub explicit_logical_modules: usize,
    pub final_modules: usize,
    pub residual_logical_modules: usize,
    pub selected_owners: usize,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct FinalModuleContent {
    pub file: String,
    pub id: String,
    pub member_names: Vec<String>,
    pub path: String,
    pub owner_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RequestedLogicalModule {
    pub id: String,
    pub target_path: String,
    pub residual: bool,
}

#[derive(Debug, Clone)]
pub struct MaterializeLogicalModulesOptions {
    pub boundary_analysis_dir: Option<PathBuf>,
    pub chunk_ids: Vec<String>,
    pub file: Option<String>,
    pub prune_other_chunks: bool,
    pub force: bool,
    pub report_out_dir: Option<PathBuf>,
    pub report_summary_path: Option<PathBuf>,
    pub selected_owner_ids_by_chunk_path: Option<PathBuf>,
    pub selected_owner_ids_by_chunk: Option<Value>,
    pub target_dir: String,
}

#[derive(Debug, Clone)]
struct LogicalRequest {
    id: String,
    target_path: String,
    residual: bool,
    members: Vec<MemberRequest>,
}

#[derive(Debug, Clone)]
struct MemberRequest {
    binding: String,
    export_name: String,
}

#[derive(Debug, Clone)]
struct TopLevelDecl {
    ordinal: usize,
    names: Vec<String>,
    item: ModuleItem,
}

#[derive(Debug, Clone)]
struct ModulePlan {
    id: String,
    target_file: String,
    explicit: bool,
    requested: LogicalRequest,
    bindings: BTreeMap<String, String>,
}

pub fn materialize_logical_modules(
    artifact: &mut JsPipelineArtifact,
    operations: &[Value],
    options: MaterializeLogicalModulesOptions,
) -> Result<LogicalModuleManifest> {
    if options.chunk_ids.is_empty() {
        bail!("materializeLogicalModules requires at least one chunkId");
    }
    let started = Instant::now();
    let target_dir = normalize_optional_relative_dir(&options.target_dir)?;
    let mut selected_chunk_ids = Vec::new();
    let mut seen = BTreeSet::new();
    for chunk_id in &options.chunk_ids {
        let normalized = normalize_relative_path(chunk_id)?;
        if seen.insert(normalized.clone()) {
            selected_chunk_ids.push(normalized);
        }
    }

    let mut report_out_dir = None;
    if let Some(dir) = &options.report_out_dir {
        let resolved = resolve_workspace_path(dir)?;
        prepare_output_dir(&resolved, options.force)?;
        report_out_dir = Some(resolved);
    }

    if options.prune_other_chunks {
        prune_artifact_to_chunk_ids(artifact, &selected_chunk_ids);
    }

    let mut reports = Vec::new();
    let mut applied = Vec::<SelectedModuleLowering>::new();
    for chunk_id in selected_chunk_ids {
        let chunk_started = Instant::now();
        let target_file = options
            .file
            .as_ref()
            .map(|file| normalize_relative_path(file))
            .transpose()?
            .or_else(|| get_chunk_entry_path(artifact, &chunk_id))
            .with_context(|| {
                format!(
                    "materializeLogicalModules could not determine entry file for chunk: {chunk_id}"
                )
            })?;
        let runtime_file = artifact
            .chunks
            .get(&chunk_id)
            .and_then(|chunk| chunk.files.get(&target_file))
            .with_context(|| {
                format!("materializeLogicalModules missing entry file for chunk: {chunk_id}")
            })?;
        let runtime_ast = runtime_file.ast.as_ref().with_context(|| {
            format!("materializeLogicalModules missing entry AST for chunk: {chunk_id}")
        })?;
        let header_lines = runtime_file.header_lines.clone();
        let source_path = runtime_file
            .metadata
            .source_path
            .clone()
            .or_else(|| artifact.chunk_source_path(&chunk_id))
            .unwrap_or_else(|| format!("{chunk_id}.js"));
        let requests = logical_requests_for_chunk(operations, &chunk_id, &target_dir)?;
        let mut explicit_requests = requests
            .iter()
            .filter(|request| !request.residual)
            .cloned()
            .collect::<Vec<_>>();
        let residual_request = requests.iter().find(|request| request.residual).cloned();

        let declarations = collect_top_level_declarations(&runtime_ast.module);
        let declaration_by_name = declarations
            .iter()
            .flat_map(|decl| decl.names.iter().map(|name| (name.clone(), decl.ordinal)))
            .collect::<BTreeMap<_, _>>();
        let uses_by_ordinal = declarations
            .iter()
            .map(|decl| (decl.ordinal, collect_referenced_idents(&decl.item)))
            .collect::<BTreeMap<_, _>>();

        let mut binding_assignment = BTreeMap::<String, usize>::new();
        let mut module_plans = Vec::new();
        for (index, request) in explicit_requests.iter_mut().enumerate() {
            let mut bindings = BTreeMap::new();
            for member in &request.members {
                bindings.insert(member.binding.clone(), member.export_name.clone());
            }
            for binding in bindings.keys() {
                if declaration_by_name.contains_key(binding) {
                    binding_assignment.insert(binding.clone(), index);
                }
            }
            module_plans.push(ModulePlan {
                id: request.id.clone(),
                target_file: target_file_for_request(&target_dir, &request.target_path)?,
                explicit: true,
                requested: request.clone(),
                bindings,
            });
        }

        close_module_bindings_over_dependencies(
            &mut module_plans,
            &mut binding_assignment,
            &declarations,
            &declaration_by_name,
            &uses_by_ordinal,
        );

        if let Some(residual) = residual_request {
            let residual_index = module_plans.len();
            let mut residual_bindings = BTreeMap::new();
            for decl in &declarations {
                for name in &decl.names {
                    if !binding_assignment.contains_key(name) {
                        binding_assignment.insert(name.clone(), residual_index);
                        residual_bindings.insert(name.clone(), name.clone());
                    }
                }
            }
            if !residual_bindings.is_empty() {
                module_plans.push(ModulePlan {
                    id: residual.id.clone(),
                    target_file: target_file_for_request(&target_dir, &residual.target_path)?,
                    explicit: false,
                    requested: residual,
                    bindings: residual_bindings,
                });
            }
        }

        let lowered = lower_chunk(LowerChunkInputs {
            runtime_ast,
            header_lines: &header_lines,
            entry_file: &target_file,
            chunk_id: &chunk_id,
            source_path: &source_path,
            declarations: &declarations,
            module_plans: &module_plans,
            binding_assignment: &binding_assignment,
        })?;

        let mut files = BTreeMap::new();
        for file in lowered.files {
            files.insert(file.path.clone(), file);
        }
        let module_extraction_state = json!({
            "kind": "js.module_extraction_state",
            "mode": "logical",
            "runtimeFile": target_file,
            "targetDir": target_dir,
        });
        artifact.chunks.insert(
            chunk_id.clone(),
            JsChunk {
                entry_file: target_file.clone(),
                files,
                metadata: ChunkMetadata {
                    source_path: Some(source_path.clone()),
                    module_extraction_state: Some(module_extraction_state),
                },
            },
        );
        if !artifact.chunk_order.contains(&chunk_id) {
            artifact.chunk_order.push(chunk_id.clone());
        }

        let selected_lowerings = lowered.applied.clone();
        if let Some(manifest) = artifact.chunk_manifests.get_mut(&chunk_id) {
            manifest.entry_file = target_file.clone();
            manifest.files = lowered
                .file_records
                .iter()
                .map(|(file, role)| ChunkFileRecord {
                    file: file.clone(),
                    role: if role == "entry" { "entry" } else { "module" },
                })
                .collect();
            manifest.logical_modules = Some(ChunkLogicalModulesSummary {
                count: module_plans.len(),
                module_ids: module_plans.iter().map(|plan| plan.id.clone()).collect(),
                target_dir: target_dir.clone(),
            });
            manifest.selected_module_lowerings = Some(selected_lowerings.clone());
        }

        let final_modules = module_plans
            .iter()
            .map(|plan| FinalModuleContent {
                file: plan.target_file.clone(),
                id: plan.id.clone(),
                member_names: plan.bindings.values().cloned().collect(),
                path: plan.requested.target_path.clone(),
                owner_ids: plan.bindings.keys().cloned().collect(),
            })
            .collect::<Vec<_>>();
        let report = LogicalChunkReport {
            chunk_id: chunk_id.clone(),
            counts: LogicalChunkCounts {
                applied: selected_lowerings.len(),
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
            timings_ms: BTreeMap::from([(
                "total".to_string(),
                chunk_started.elapsed().as_secs_f64() * 1000.0,
            )]),
        };
        if let Some(report_out_dir) = &report_out_dir {
            let report_path = report_out_dir.join(format!("{chunk_id}.json"));
            if let Some(parent) = report_path.parent() {
                fs::create_dir_all(parent)?;
            }
            fs::write(&report_path, serde_json::to_string_pretty(&report)? + "\n")?;
        }
        applied.extend(selected_lowerings);
        reports.push(report);
    }

    update_root_manifest(artifact, &reports, &applied);
    let manifest = LogicalModuleManifest {
        chunk_count: reports.len(),
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
        duration_ms: started.elapsed().as_secs_f64() * 1000.0,
        kind: "js.logical_module_manifest",
        report_out_dir: report_out_dir
            .as_ref()
            .map(|path| path.to_string_lossy().replace('\\', "/")),
        schema_version: 1,
    };

    if let Some(summary_path) = options.report_summary_path {
        let resolved = resolve_workspace_path(&summary_path)?;
        if let Some(parent) = resolved.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::write(resolved, serde_json::to_string_pretty(&manifest)? + "\n")?;
    }
    if let Some(path) = options.selected_owner_ids_by_chunk_path {
        let resolved = resolve_workspace_path(&path)?;
        if let Some(parent) = resolved.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::write(
            resolved,
            serde_json::to_string_pretty(&json!({
                "chunkOwnerIds": options.selected_owner_ids_by_chunk.unwrap_or_else(|| json!({})),
                "kind": "js.selected_owner_ids_cache",
                "schemaVersion": 1,
            }))? + "\n",
        )?;
    }
    let _ = options.boundary_analysis_dir;
    Ok(manifest)
}

struct LoweredChunk {
    files: Vec<JsFile>,
    file_records: Vec<(String, String)>,
    applied: Vec<SelectedModuleLowering>,
}

struct LowerChunkInputs<'a> {
    runtime_ast: &'a ParsedJsModule,
    header_lines: &'a [String],
    entry_file: &'a str,
    chunk_id: &'a str,
    source_path: &'a str,
    declarations: &'a [TopLevelDecl],
    module_plans: &'a [ModulePlan],
    binding_assignment: &'a BTreeMap<String, usize>,
}

fn lower_chunk(inputs: LowerChunkInputs<'_>) -> Result<LoweredChunk> {
    let LowerChunkInputs {
        runtime_ast,
        header_lines,
        entry_file,
        chunk_id,
        source_path,
        declarations,
        module_plans,
        binding_assignment,
    } = inputs;
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

    let requires_init_by_module =
        init_required_modules(declarations, binding_assignment, module_plans.len());
    let mut selected_by_module = BTreeMap::<usize, Vec<ModuleItem>>::new();
    let mut selected_exports_by_module = BTreeMap::<usize, BTreeMap<String, String>>::new();
    for (module_index, plan) in module_plans.iter().enumerate() {
        if plan.bindings.is_empty() {
            continue;
        }
        selected_exports_by_module.insert(module_index, plan.bindings.clone());
    }

    let mut entry_body = Vec::new();
    let mut called_init_modules = BTreeSet::<usize>::new();
    let import_insert_index = runtime_ast
        .module
        .body
        .iter()
        .take_while(|item| matches!(item, ModuleItem::ModuleDecl(ModuleDecl::Import(_))))
        .count();
    for (ordinal, item) in runtime_ast.module.body.iter().enumerate() {
        if !selected_ordinals.contains(&ordinal) {
            entry_body.push(item.clone());
            continue;
        }
        let selected_module_index = assigned_module_for_item(item, binding_assignment);
        let mut remaining =
            remaining_item_after_selection(item, binding_assignment, &mut selected_by_module)?;
        entry_body.append(&mut remaining);
        if let Some(module_index) = selected_module_index
            && requires_init_by_module.contains(&module_index)
            && called_init_modules.insert(module_index)
        {
            entry_body.push(init_call_statement(&init_name_for_plan(
                &module_plans[module_index],
            )));
        }
    }
    let mut entry_imports = Vec::<ModuleItem>::new();
    for (module_index, plan) in module_plans.iter().enumerate() {
        if plan.bindings.is_empty() {
            continue;
        }
        let mut bindings = plan.bindings.clone();
        if requires_init_by_module.contains(&module_index) {
            let init_name = init_name_for_plan(plan);
            bindings.insert(init_name.clone(), init_name);
        }
        entry_imports.push(import_decl_for_plan(
            entry_file,
            &plan.target_file,
            &bindings,
        ));
    }
    for import in entry_imports.into_iter().rev() {
        entry_body.insert(import_insert_index, import);
    }
    for export in entry_exports_for_moved_bindings(runtime_ast, binding_assignment) {
        entry_body.push(export);
    }

    let mut files = vec![JsFile {
        path: entry_file.to_string(),
        content: None,
        ast: Some(ParsedJsModule {
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
            role: Some("entry".to_string()),
            source_path: Some(source_path.to_string()),
            ..Default::default()
        },
    }];
    let mut file_records = vec![(entry_file.to_string(), "entry".to_string())];
    let mut applied = Vec::new();

    for (index, plan) in module_plans.iter().enumerate() {
        let mut body = selected_by_module.remove(&index).unwrap_or_default();
        let local_renames = naturalize_module_body(&mut body, plan);
        let mut module_imports = cross_module_imports_for_body(
            index,
            &plan.target_file,
            &body,
            module_plans,
            binding_assignment,
        );
        module_imports.append(&mut body);
        body = module_imports;
        rewrite_runtime_sources_for_target(&mut body, &plan.target_file);
        if let Some(exports) = selected_exports_by_module.get(&index) {
            let exports = final_module_exports(exports, &local_renames);
            if requires_init_by_module.contains(&index) {
                body = initialized_module_body(plan, body, &exports)?;
            } else {
                body.push(export_named_for_bindings(&exports));
            }
        }
        let owner_ids = plan.bindings.keys().cloned().collect::<Vec<_>>();
        let header = vec![
            LOWERING_FILE_PRAGMA.to_string(),
            LOWERING_GENERATOR_HEADER.to_string(),
            format!(
                "// Selected-module lowered region; original owners: {}.",
                owner_ids.join(", ")
            ),
        ];
        files.push(JsFile {
            path: plan.target_file.clone(),
            content: None,
            ast: Some(ParsedJsModule {
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
                role: Some("module".to_string()),
                source_path: Some(source_path.to_string()),
                generated_stage: Some("selected_module_lowering".to_string()),
            },
        });
        file_records.push((plan.target_file.clone(), "module".to_string()));
        applied.push(SelectedModuleLowering {
            chunk_id: chunk_id.to_string(),
            exported_names: plan.bindings.values().cloned().collect(),
            file: entry_file.to_string(),
            id: plan.id.clone(),
            operation: "lower_selected_module_region",
            owner_ids: plan.bindings.keys().cloned().collect(),
            target_file: plan.target_file.clone(),
        });
    }

    Ok(LoweredChunk {
        files,
        file_records,
        applied,
    })
}

fn logical_requests_for_chunk(
    operations: &[Value],
    chunk_id: &str,
    target_dir: &str,
) -> Result<Vec<LogicalRequest>> {
    let mut requests = Vec::new();
    for op in operations {
        let operation = op.get("operation").and_then(Value::as_str);
        if !matches!(
            operation,
            Some("define_logical_module" | "define_residual_module")
        ) {
            continue;
        }
        let selector_chunk = op
            .get("selector")
            .and_then(|selector| selector.get("chunkId"))
            .and_then(Value::as_str);
        if selector_chunk != Some(chunk_id) {
            continue;
        }
        let id = op
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or(operation.unwrap_or("logical_module"))
            .to_string();
        let target_path = op
            .get("target")
            .and_then(|target| target.get("path"))
            .and_then(Value::as_str)
            .unwrap_or("residual/unhandled")
            .to_string();
        let residual = operation == Some("define_residual_module");
        let members: Vec<MemberRequest> = op
            .get("members")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(|member| {
                let binding = member
                    .get("selector")
                    .and_then(|selector| selector.get("binding"))
                    .and_then(|binding| binding.get("name"))
                    .and_then(Value::as_str)?;
                let export_name = member
                    .get("name")
                    .and_then(Value::as_str)
                    .unwrap_or(binding)
                    .to_string();
                Some(MemberRequest {
                    binding: binding.to_string(),
                    export_name,
                })
            })
            .collect();
        reject_duplicate_export_names(operation.unwrap_or("logical_module"), &id, &members)?;
        requests.push(LogicalRequest {
            id,
            target_path,
            residual,
            members,
        });
    }
    if requests.is_empty() {
        requests.push(LogicalRequest {
            id: "logical_module_0".to_string(),
            target_path: format!("{target_dir}/unhandled"),
            residual: true,
            members: Vec::new(),
        });
    }
    Ok(requests)
}

fn collect_top_level_declarations(module: &Module) -> Vec<TopLevelDecl> {
    let mut out = Vec::new();
    for (ordinal, item) in module.body.iter().enumerate() {
        let names = top_level_declaration_names(item);
        if names.is_empty() {
            continue;
        }
        out.push(TopLevelDecl {
            ordinal,
            names,
            item: item.clone(),
        });
    }
    out
}

fn top_level_declaration_names(item: &ModuleItem) -> Vec<String> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => declaration_names(decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
            declaration_names(&export_decl.decl)
        }
        _ => Vec::new(),
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

fn collect_referenced_idents(item: &ModuleItem) -> BTreeSet<String> {
    let mut collector = RefCollector::default();
    item.visit_with(&mut collector);
    collector.names
}

#[derive(Default)]
struct RefCollector {
    names: BTreeSet<String>,
}

impl Visit for RefCollector {
    fn visit_ident(&mut self, node: &Ident) {
        self.names.insert(node.sym.to_string());
    }

    fn visit_binding_ident(&mut self, _node: &BindingIdent) {}

    fn visit_import_decl(&mut self, _node: &ImportDecl) {}
}

fn close_module_bindings_over_dependencies(
    module_plans: &mut [ModulePlan],
    binding_assignment: &mut BTreeMap<String, usize>,
    declarations: &[TopLevelDecl],
    declaration_by_name: &BTreeMap<String, usize>,
    uses_by_ordinal: &BTreeMap<usize, BTreeSet<String>>,
) {
    let declaration_by_ordinal = declarations
        .iter()
        .map(|decl| (decl.ordinal, decl))
        .collect::<BTreeMap<_, _>>();
    for (module_index, plan) in module_plans.iter_mut().enumerate() {
        expand_plan_to_transitive_dependencies(
            plan,
            module_index,
            binding_assignment,
            declaration_by_name,
            &declaration_by_ordinal,
            uses_by_ordinal,
        );
    }
}

fn expand_plan_to_transitive_dependencies(
    plan: &mut ModulePlan,
    module_index: usize,
    binding_assignment: &mut BTreeMap<String, usize>,
    declaration_by_name: &BTreeMap<String, usize>,
    declaration_by_ordinal: &BTreeMap<usize, &TopLevelDecl>,
    uses_by_ordinal: &BTreeMap<usize, BTreeSet<String>>,
) {
    let mut queue = plan
        .bindings
        .keys()
        .filter_map(|name| declaration_by_name.get(name).copied())
        .collect::<VecDeque<_>>();
    while let Some(ordinal) = queue.pop_front() {
        let Some(uses) = uses_by_ordinal.get(&ordinal) else {
            continue;
        };
        for used in uses {
            let Some(dep_ordinal) = declaration_by_name.get(used).copied() else {
                continue;
            };
            if binding_assignment.contains_key(used) {
                continue;
            }
            binding_assignment.insert(used.clone(), module_index);
            plan.bindings.insert(used.clone(), used.clone());
            if let Some(dep_decl) = declaration_by_ordinal.get(&dep_ordinal) {
                for dep_name in &dep_decl.names {
                    binding_assignment.insert(dep_name.clone(), module_index);
                    plan.bindings.insert(dep_name.clone(), dep_name.clone());
                }
            }
            queue.push_back(dep_ordinal);
        }
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

fn init_required_modules(
    declarations: &[TopLevelDecl],
    binding_assignment: &BTreeMap<String, usize>,
    module_count: usize,
) -> BTreeSet<usize> {
    let mut required = BTreeSet::new();
    for decl in declarations {
        let Some(module_index) = assigned_module_for_names(&decl.names, binding_assignment) else {
            continue;
        };
        if module_index >= module_count {
            continue;
        }
        if item_requires_init_wrapper_for_module(&decl.item, module_index, binding_assignment) {
            required.insert(module_index);
        }
    }
    required
}

fn item_requires_init_wrapper_for_module(
    item: &ModuleItem,
    module_index: usize,
    binding_assignment: &BTreeMap<String, usize>,
) -> bool {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => {
            var_requires_init_wrapper_for_module(var, module_index, binding_assignment)
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => match &export_decl.decl {
            Decl::Var(var) => {
                var_requires_init_wrapper_for_module(var, module_index, binding_assignment)
            }
            _ => false,
        },
        _ => false,
    }
}

fn var_requires_init_wrapper_for_module(
    var: &VarDecl,
    module_index: usize,
    binding_assignment: &BTreeMap<String, usize>,
) -> bool {
    if var.kind == VarDeclKind::Var {
        return false;
    }
    var.decls.iter().any(|declarator| {
        let names = binding_names(&declarator.name);
        if !names
            .iter()
            .any(|name| binding_assignment.get(name) == Some(&module_index))
        {
            return false;
        }
        declarator
            .init
            .as_deref()
            .is_some_and(|init| !is_plain_import_safe_initializer(init))
    })
}

fn is_plain_import_safe_initializer(expr: &Expr) -> bool {
    match expr {
        Expr::Lit(_) | Expr::Ident(_) | Expr::Fn(_) | Expr::Arrow(_) | Expr::Class(_) => true,
        Expr::Unary(unary) => is_plain_import_safe_initializer(&unary.arg),
        Expr::Bin(binary) => {
            is_plain_import_safe_initializer(&binary.left)
                && is_plain_import_safe_initializer(&binary.right)
        }
        Expr::Tpl(template) => template
            .exprs
            .iter()
            .all(|expr| is_plain_import_safe_initializer(expr)),
        Expr::Array(array) => array
            .elems
            .iter()
            .flatten()
            .all(|element| is_plain_import_safe_initializer(&element.expr)),
        Expr::Object(object) => object.props.iter().all(|prop| match prop {
            PropOrSpread::Spread(spread) => is_plain_import_safe_initializer(&spread.expr),
            PropOrSpread::Prop(prop) => match &**prop {
                Prop::KeyValue(key_value) => is_plain_import_safe_initializer(&key_value.value),
                Prop::Shorthand(_) => true,
                Prop::Method(_) | Prop::Getter(_) | Prop::Setter(_) => true,
                Prop::Assign(assign) => is_plain_import_safe_initializer(&assign.value),
            },
        }),
        _ => false,
    }
}

fn assigned_module_for_item(
    item: &ModuleItem,
    binding_assignment: &BTreeMap<String, usize>,
) -> Option<usize> {
    assigned_module_for_names(&top_level_declaration_names(item), binding_assignment)
}

fn cross_module_imports_for_body(
    module_index: usize,
    from_file: &str,
    body: &[ModuleItem],
    module_plans: &[ModulePlan],
    binding_assignment: &BTreeMap<String, usize>,
) -> Vec<ModuleItem> {
    let mut imports_by_provider = BTreeMap::<usize, BTreeMap<String, String>>::new();
    for item in body {
        for name in collect_referenced_idents(item) {
            let Some(provider_index) = binding_assignment.get(&name).copied() else {
                continue;
            };
            if provider_index == module_index {
                continue;
            }
            let Some(provider_plan) = module_plans.get(provider_index) else {
                continue;
            };
            let Some(exported_name) = provider_plan.bindings.get(&name) else {
                continue;
            };
            imports_by_provider
                .entry(provider_index)
                .or_default()
                .insert(name, exported_name.clone());
        }
    }
    imports_by_provider
        .into_iter()
        .filter_map(|(provider_index, bindings)| {
            module_plans
                .get(provider_index)
                .map(|provider| import_decl_for_plan(from_file, &provider.target_file, &bindings))
        })
        .collect()
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
    for (local, exported) in &plan.bindings {
        if local != exported && is_identifier_like(exported) {
            renames.insert(local.clone(), exported.clone());
        }
    }
    for item in body.iter_mut() {
        collect_naturalization_renames_from_item(item, &mut renames);
    }
    if !renames.is_empty() {
        for item in body.iter_mut() {
            item.visit_mut_with(&mut IdentifierRenamer {
                renames: renames.clone(),
            });
        }
    }
    for item in body.iter_mut() {
        item.visit_mut_with(&mut ShorthandNaturalizer);
    }
    renames
}

fn collect_naturalization_renames_from_item(
    item: &mut ModuleItem,
    renames: &mut BTreeMap<String, String>,
) {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Fn(function))) => {
            collect_naturalization_renames_from_function(&mut function.function, renames);
        }
        ModuleItem::Stmt(Stmt::Decl(Decl::Class(class))) => {
            collect_naturalization_renames_from_class(&mut class.class, renames);
        }
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => {
            for declarator in &mut var.decls {
                if let Some(init) = declarator.init.as_mut() {
                    collect_naturalization_renames_from_expr(init, renames);
                }
            }
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
            match &mut export_decl.decl {
                Decl::Fn(function) => {
                    collect_naturalization_renames_from_function(&mut function.function, renames);
                }
                Decl::Class(class) => {
                    collect_naturalization_renames_from_class(&mut class.class, renames);
                }
                Decl::Var(var) => {
                    for declarator in &mut var.decls {
                        if let Some(init) = declarator.init.as_mut() {
                            collect_naturalization_renames_from_expr(init, renames);
                        }
                    }
                }
                _ => {}
            }
        }
        _ => {}
    }
}

fn collect_naturalization_renames_from_expr(
    expr: &mut Expr,
    renames: &mut BTreeMap<String, String>,
) {
    match expr {
        Expr::Fn(function) => {
            collect_naturalization_renames_from_function(&mut function.function, renames)
        }
        Expr::Arrow(arrow) => {
            for param in &mut arrow.params {
                collect_naturalization_renames_from_pattern(param, renames);
            }
        }
        Expr::Class(class) => collect_naturalization_renames_from_class(&mut class.class, renames),
        _ => {}
    }
}

fn collect_naturalization_renames_from_function(
    function: &mut Box<Function>,
    renames: &mut BTreeMap<String, String>,
) {
    for param in &mut function.params {
        collect_naturalization_renames_from_pattern(&mut param.pat, renames);
    }
    let Some(body) = function.body.as_mut() else {
        return;
    };
    collect_return_object_alias_renames(&body.stmts, renames);
}

fn collect_naturalization_renames_from_class(
    class: &mut Box<Class>,
    renames: &mut BTreeMap<String, String>,
) {
    for member in &mut class.body {
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

fn collect_naturalization_renames_from_pattern(
    pat: &mut Pat,
    renames: &mut BTreeMap<String, String>,
) {
    match pat {
        Pat::Object(object) => {
            for prop in &mut object.props {
                match prop {
                    ObjectPatProp::KeyValue(key_value) => {
                        if let PropName::Ident(key) = &key_value.key
                            && let Pat::Ident(value) = &*key_value.value
                        {
                            let from = value.id.sym.to_string();
                            let to = key.sym.to_string();
                            if from != to && is_identifier_like(&to) {
                                renames.insert(from, to.clone());
                                *prop = ObjectPatProp::Assign(AssignPatProp {
                                    span: DUMMY_SP,
                                    key: BindingIdent {
                                        id: Ident::new_no_ctxt(to.into(), DUMMY_SP),
                                        type_ann: None,
                                    },
                                    value: None,
                                });
                            }
                        }
                    }
                    ObjectPatProp::Assign(_) => {}
                    ObjectPatProp::Rest(rest) => {
                        collect_naturalization_renames_from_pattern(&mut rest.arg, renames);
                    }
                }
            }
        }
        Pat::Array(array) => {
            for elem in array.elems.iter_mut().flatten() {
                collect_naturalization_renames_from_pattern(elem, renames);
            }
        }
        Pat::Assign(assign) => {
            collect_naturalization_renames_from_pattern(&mut assign.left, renames)
        }
        Pat::Rest(rest) => collect_naturalization_renames_from_pattern(&mut rest.arg, renames),
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

#[derive(Clone)]
struct IdentifierRenamer {
    renames: BTreeMap<String, String>,
}

impl VisitMut for IdentifierRenamer {
    fn visit_mut_ident(&mut self, ident: &mut Ident) {
        if let Some(to) = self.renames.get(&ident.sym.to_string()) {
            ident.sym = to.clone().into();
        }
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
}

struct ShorthandNaturalizer;

impl VisitMut for ShorthandNaturalizer {
    fn visit_mut_object_pat(&mut self, object: &mut ObjectPat) {
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
        object.visit_mut_children_with(self);
    }

    fn visit_mut_object_lit(&mut self, object: &mut ObjectLit) {
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
        object.visit_mut_children_with(self);
    }
}

fn rewrite_runtime_sources_for_target(body: &mut [ModuleItem], target_file: &str) {
    let mut rewriter = RuntimeSourceRewriter {
        target_file: target_file.to_string(),
    };
    for item in body {
        item.visit_mut_with(&mut rewriter);
    }
}

struct RuntimeSourceRewriter {
    target_file: String,
}

impl RuntimeSourceRewriter {
    fn rewrite(&self, source: &str) -> String {
        let original = normalize_relative_path(source).unwrap_or_else(|_| source.to_string());
        let target_dir = Path::new(&self.target_file)
            .parent()
            .and_then(Path::to_str)
            .unwrap_or("")
            .replace('\\', "/");
        let runtime_dir = if target_dir == "modules" || target_dir.starts_with("modules/") {
            String::new()
        } else {
            self.target_file
                .split_once("/modules/")
                .map(|(chunk_root, _)| chunk_root.to_string())
                .unwrap_or_else(|| target_dir.clone())
        };
        let original_abs = posix_join(&[&runtime_dir, &original]);
        let mut rel = posix_relative(&target_dir, &original_abs);
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

fn initialized_module_body(
    plan: &ModulePlan,
    body: Vec<ModuleItem>,
    exports: &BTreeMap<String, String>,
) -> Result<Vec<ModuleItem>> {
    let mut out = Vec::new();
    let mut init_statements = Vec::new();
    for item in body {
        match item {
            ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => {
                push_initialized_var_decl(*var, &mut out, &mut init_statements)?;
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => match export_decl.decl {
                Decl::Var(var) => {
                    push_initialized_var_decl(*var, &mut out, &mut init_statements)?;
                }
                decl => out.push(ModuleItem::Stmt(Stmt::Decl(decl))),
            },
            other => out.push(other),
        }
    }
    out.push(export_init_function(
        &init_name_for_plan(plan),
        init_statements,
    ));
    out.push(export_named_for_bindings(exports));
    Ok(out)
}

fn push_initialized_var_decl(
    var: VarDecl,
    out: &mut Vec<ModuleItem>,
    init_statements: &mut Vec<Stmt>,
) -> Result<()> {
    let mut declarations = Vec::new();
    for declarator in var.decls {
        if let Some(init) = declarator.init.clone() {
            init_statements.push(assignment_statement_for_declarator(&declarator, init)?);
        }
        declarations.push(VarDeclarator {
            init: None,
            ..declarator
        });
    }
    out.push(ModuleItem::Stmt(Stmt::Decl(Decl::Var(Box::new(VarDecl {
        span: var.span,
        ctxt: var.ctxt,
        kind: VarDeclKind::Let,
        declare: var.declare,
        decls: declarations,
    })))));
    Ok(())
}

fn assignment_statement_for_declarator(
    declarator: &VarDeclarator,
    init: Box<Expr>,
) -> Result<Stmt> {
    let left: AssignTarget = declarator.name.clone().try_into().map_err(|_| {
        anyhow::anyhow!("init-wrapper lowering only supports assignable binding patterns")
    })?;
    Ok(Stmt::Expr(ExprStmt {
        span: DUMMY_SP,
        expr: Box::new(Expr::Assign(AssignExpr {
            span: DUMMY_SP,
            op: AssignOp::Assign,
            left,
            right: init,
        })),
    }))
}

fn export_init_function(name: &str, statements: Vec<Stmt>) -> ModuleItem {
    ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
        span: DUMMY_SP,
        decl: Decl::Fn(FnDecl {
            ident: Ident::new_no_ctxt(name.into(), DUMMY_SP),
            declare: false,
            function: Box::new(Function {
                params: Vec::new(),
                decorators: Vec::new(),
                span: DUMMY_SP,
                ctxt: SyntaxContext::empty(),
                body: Some(BlockStmt {
                    span: DUMMY_SP,
                    ctxt: SyntaxContext::empty(),
                    stmts: statements,
                }),
                is_generator: false,
                is_async: false,
                type_params: None,
                return_type: None,
            }),
        }),
    }))
}

fn init_call_statement(name: &str) -> ModuleItem {
    ModuleItem::Stmt(Stmt::Expr(ExprStmt {
        span: DUMMY_SP,
        expr: Box::new(Expr::Call(CallExpr {
            span: DUMMY_SP,
            ctxt: SyntaxContext::empty(),
            callee: Callee::Expr(Box::new(Expr::Ident(Ident::new_no_ctxt(
                name.into(),
                DUMMY_SP,
            )))),
            args: Vec::new(),
            type_args: None,
        })),
    }))
}

fn init_name_for_plan(plan: &ModulePlan) -> String {
    sanitize_identifier(&format!(
        "__dt_generated_init__{}",
        plan.requested.target_path
    ))
}

fn sanitize_identifier(value: &str) -> String {
    let mut out = String::new();
    for (index, ch) in value.chars().enumerate() {
        let valid = ch == '_' || ch == '$' || ch.is_ascii_alphanumeric();
        if index == 0 && ch.is_ascii_digit() {
            out.push('_');
        }
        out.push(if valid { ch } else { '_' });
    }
    if out.is_empty() { "_".to_string() } else { out }
}

fn target_file_for_request(target_dir: &str, target_path: &str) -> Result<String> {
    let normalized = normalize_relative_path(target_path)?;
    let with_ext = if normalized.ends_with(".js") {
        normalized
    } else {
        format!("{normalized}.js")
    };
    Ok(posix_join(&[target_dir, &with_ext]))
}

fn normalize_optional_relative_dir(value: &str) -> Result<String> {
    normalize_relative_path(value)
}

fn remaining_item_after_selection(
    item: &ModuleItem,
    binding_assignment: &BTreeMap<String, usize>,
    selected_by_module: &mut BTreeMap<usize, Vec<ModuleItem>>,
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
                    selected_by_module
                        .entry(module_index)
                        .or_default()
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
                selected_by_module
                    .entry(module_index)
                    .or_default()
                    .push(item.clone());
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
    selected_by_module: &mut BTreeMap<usize, Vec<ModuleItem>>,
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
            selected_by_module
                .entry(module_index)
                .or_default()
                .push(ModuleItem::Stmt(Stmt::Decl(Decl::Var(Box::new(
                    selected_var,
                )))));
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
    let mut rel = posix_relative(&from_dir, target_file);
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
    runtime_ast: &ParsedJsModule,
    binding_assignment: &BTreeMap<String, usize>,
) -> Vec<ModuleItem> {
    let mut exports = BTreeMap::<String, String>::new();
    for item in &runtime_ast.module.body {
        if let ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) = item {
            for name in declaration_names(&export_decl.decl) {
                if binding_assignment.contains_key(&name) {
                    exports.insert(name.clone(), name);
                }
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
    let selected = selected.iter().cloned().collect::<BTreeSet<_>>();
    artifact
        .chunks
        .retain(|chunk_id, _| selected.contains(chunk_id));
    artifact
        .chunk_order
        .retain(|chunk_id| selected.contains(chunk_id));
    artifact
        .chunk_manifests
        .retain(|chunk_id, _| selected.contains(chunk_id));
    if let Some(root_manifest) = &mut artifact.root_manifest {
        root_manifest
            .chunks
            .retain(|chunk| selected.contains(&chunk.chunk_id));
        root_manifest.counts.chunks = root_manifest.chunks.len();
    }
}

fn update_root_manifest(
    artifact: &mut JsPipelineArtifact,
    reports: &[LogicalChunkReport],
    applied: &[SelectedModuleLowering],
) {
    if artifact.root_manifest.is_none() {
        artifact.root_manifest = Some(ArtifactManifest {
            schema_version: 1,
            counts: ArtifactCounts {
                chunks: artifact.list_chunk_ids().len(),
                kept_top_level_declaration_owners: 0,
                top_level_side_effects: 0,
                export_aliases: 0,
                unresolved_exports: 0,
                selected_module_lowerings: None,
                extra: Default::default(),
            },
            chunks: artifact
                .list_chunk_ids()
                .into_iter()
                .map(|chunk_id| ::artifact::ArtifactChunkRecord {
                    source_path: artifact
                        .chunk_source_path(&chunk_id)
                        .unwrap_or_else(|| format!("{chunk_id}.js")),
                    chunk_id,
                })
                .collect(),
            logical_modules: None,
            selected_module_lowerings: None,
            extra: Default::default(),
        });
    }
    let Some(root_manifest) = &mut artifact.root_manifest else {
        return;
    };
    root_manifest.counts.selected_module_lowerings = Some(applied.len());
    root_manifest.logical_modules = Some(RootLogicalModulesSummary {
        chunk_count: reports.len(),
        module_count: reports.iter().map(|r| r.counts.final_modules).sum(),
    });
    root_manifest.selected_module_lowerings = Some(applied.to_vec());
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
