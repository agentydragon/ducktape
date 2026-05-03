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
    SelectedModuleLowering, get_chunk_entry_path, manifest_relative_path, normalize_relative_path,
    path_to_posix, posix_join, posix_relative,
};
use js_ast::{ParsedJsModule, set_str_value, str_value};

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
}

#[derive(Debug, Clone)]
struct MemberRequest {
    binding: String,
    export_name: String,
    /// When `Some`, the member's source is an import specifier in the
    /// source chunk (not a top-level decl). The materializer rewrites
    /// it to a re-import in the destination module.
    import: Option<ImportSpecifierInfo>,
}

#[derive(Debug, Clone)]
struct ImportSpecifierInfo {
    source: String,
    imported: String,
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
    /// `binding.kind: ImportSpecifier` members. Each entry produces an
    /// `import { <imported> as <export_name> } from "<source>";` plus
    /// `export { <export_name> };` in the destination module body. The
    /// local in the source chunk's import is a different name; it
    /// stays untouched so the source-chunk consumers continue to work.
    import_members: Vec<ImportSpecifierMember>,
}

#[derive(Debug, Clone)]
struct ImportSpecifierMember {
    export_name: String,
    source: String,
    imported: String,
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
        prepare_output_dir(dir, options.force)?;
        report_out_dir = Some(dir.clone());
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
            let mut import_members = Vec::new();
            for member in &request.members {
                if let Some(import) = &member.import {
                    // ImportSpecifier-bound: emit a re-import in the
                    // destination module, not a top-level decl move.
                    import_members.push(ImportSpecifierMember {
                        export_name: member.export_name.clone(),
                        source: import.source.clone(),
                        imported: import.imported.clone(),
                    });
                } else {
                    bindings.insert(member.binding.clone(), member.export_name.clone());
                }
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
                import_members,
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
                    import_members: Vec::new(),
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
        report_out_dir: report_out_dir.as_ref().map(|path| {
            options
                .report_summary_path
                .as_ref()
                .map_or_else(|| path_to_posix(path), |s| manifest_relative_path(s, path))
        }),
        schema_version: 1,
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
    let mut occupied = collect_occupied_local_names(&entry_body);
    let mut body_renames = BTreeMap::<String, String>::new();
    for (module_index, plan) in module_plans.iter().enumerate() {
        if plan.bindings.is_empty() {
            continue;
        }
        let mut emit_renames = BTreeMap::<String, String>::new();
        let mut resolved =
            disambiguate_import_locals(&plan.bindings, &mut occupied, &mut emit_renames);
        if requires_init_by_module.contains(&module_index) {
            let init_name = init_name_for_plan(plan);
            resolved.insert(init_name.clone(), init_name);
        }
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
        entry_imports.push(import_decl_for_plan(
            entry_file,
            &plan.target_file,
            &resolved,
        ));
    }
    if !body_renames.is_empty() {
        // Re-exports `export { local }` (without `from`) collapse `local`
        // and the public exported name into a single ident. Renaming the
        // orig would also rename the public name, breaking downstream
        // consumers — so rewrite them to `export { fresh as local }`
        // before the generic renamer visits the rest.
        for item in entry_body.iter_mut() {
            preserve_export_specifier_names(item, &body_renames);
        }
        let mut renamer = IdentifierRenamer {
            renames: body_renames,
        };
        for item in entry_body.iter_mut() {
            item.visit_mut_with(&mut renamer);
        }
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
        // ImportSpecifier-bound members: emit a re-import in the
        // destination module for each, plus mirror the export. The
        // re-imports go at the top of the body, after the cross-module
        // imports already inserted.
        let mut import_member_exports = BTreeMap::<String, String>::new();
        if !plan.import_members.is_empty() {
            let import_count = body
                .iter()
                .take_while(|item| matches!(item, ModuleItem::ModuleDecl(ModuleDecl::Import(_))))
                .count();
            for (offset, member) in plan.import_members.iter().enumerate() {
                let decl = import_specifier_member_decl(
                    entry_file,
                    &plan.target_file,
                    &member.source,
                    &member.imported,
                    &member.export_name,
                );
                body.insert(import_count + offset, decl);
                import_member_exports
                    .insert(member.export_name.clone(), member.export_name.clone());
            }
        }
        let exports_have_top_level_decls = selected_exports_by_module.contains_key(&index);
        if exports_have_top_level_decls {
            let exports = selected_exports_by_module.get(&index).expect("checked");
            let mut exports = final_module_exports(exports, &local_renames);
            exports.extend(
                import_member_exports
                    .iter()
                    .map(|(k, v)| (k.clone(), v.clone())),
            );
            if requires_init_by_module.contains(&index) {
                body = initialized_module_body(plan, body, &exports)?;
            } else {
                body.push(export_named_for_bindings(&exports));
            }
        } else if !import_member_exports.is_empty() {
            body.push(export_named_for_bindings(&import_member_exports));
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
                let selector = member.get("selector")?;
                let binding_node = selector.get("binding")?;
                let binding = binding_node.get("name").and_then(Value::as_str)?;
                let export_name = member
                    .get("name")
                    .and_then(Value::as_str)
                    .unwrap_or(binding)
                    .to_string();
                // ImportSpecifier-bound members carry an `import` block
                // pointing at the source chunk's import statement; the
                // materializer turns the rename into a re-import in the
                // destination module.
                let import = if binding_node.get("kind").and_then(Value::as_str)
                    == Some("ImportSpecifier")
                {
                    selector.get("import").and_then(|info| {
                        let source = info.get("source").and_then(Value::as_str)?;
                        let imported = info.get("imported").and_then(Value::as_str)?;
                        Some(ImportSpecifierInfo {
                            source: source.to_string(),
                            imported: imported.to_string(),
                        })
                    })
                } else {
                    None
                };
                Some(MemberRequest {
                    binding: binding.to_string(),
                    export_name,
                    import,
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
            target_path: posix_join(&[target_dir, "unhandled"]),
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
                // Sibling declarators may already be claimed by another plan's
                // explicit spec or earlier closure. Don't steal them — split_var_decl
                // already routes per-declarator destinations correctly.
                for dep_name in &dep_decl.names {
                    if !binding_assignment.contains_key(dep_name) {
                        binding_assignment.insert(dep_name.clone(), module_index);
                        plan.bindings.insert(dep_name.clone(), dep_name.clone());
                    }
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
    let mut heuristic = BTreeMap::<String, String>::new();
    for item in body.iter() {
        collect_naturalization_renames_from_item(item, &mut heuristic);
    }
    let renames = drop_target_collisions(renames, heuristic);
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
        // Original import sources in lowered module bodies are chunk-root-relative;
        // the lowered file lives at <target_dir>/<basename> within the chunk, so the
        // rewritten specifier walks up out of target_dir to chunk root.
        let mut rel = posix_relative(&target_dir, &original);
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
    if value.is_empty() {
        return Ok(String::new());
    }
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

/// Names occupying the file-scope binding namespace of `body`.
///
/// Used to disambiguate consumer-side `import { exportedName as localName }`
/// emissions whose `localName` would collide with another binding in the
/// same scope (e.g. a surviving import or top-level declaration that
/// already uses the scrambled name). `export { name }` re-exports without
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

/// Map plan-side `local -> exported` to `actual_local -> exported`,
/// minting a fresh `<local>$N` whenever the requested local would collide
/// with `occupied`. Records original-to-fresh entries in `renames` so the
/// caller can rewrite consumer-body references after emission.
fn disambiguate_import_locals(
    bindings: &BTreeMap<String, String>,
    occupied: &mut BTreeSet<String>,
    renames: &mut BTreeMap<String, String>,
) -> BTreeMap<String, String> {
    bindings
        .iter()
        .map(|(local, exported)| {
            let actual = if occupied.contains(local) {
                let fresh = mint_fresh_local_name(local, occupied);
                renames.insert(local.clone(), fresh.clone());
                fresh
            } else {
                local.clone()
            };
            occupied.insert(actual.clone());
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

/// Build an `import { <imported> as <local> } from "<rewritten_source>";`
/// for an `ImportSpecifier` member moved into a destination module.
/// The spec's `import.source` is relative to the source chunk's entry
/// file; the destination's relative path differs, so re-resolve.
fn import_specifier_member_decl(
    source_chunk_entry_file: &str,
    destination_target_file: &str,
    import_source: &str,
    imported: &str,
    local: &str,
) -> ModuleItem {
    let source_dir = std::path::Path::new(source_chunk_entry_file)
        .parent()
        .and_then(std::path::Path::to_str)
        .unwrap_or("")
        .replace('\\', "/");
    let absolute_target = posix_join(&[&source_dir, import_source]);
    let absolute_normalized = normalize_relative_path(&absolute_target).unwrap_or(absolute_target);
    let rewritten = relative_source(destination_target_file, &absolute_normalized);
    ModuleItem::ModuleDecl(ModuleDecl::Import(ImportDecl {
        span: DUMMY_SP,
        specifiers: vec![ImportSpecifier::Named(ImportNamedSpecifier {
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
        })],
        src: Box::new(Str {
            span: DUMMY_SP,
            value: rewritten.into(),
            raw: None,
        }),
        type_only: false,
        with: None,
        phase: ImportPhase::Evaluation,
    }))
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
            scrambled_identifier_frequencies: None,
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
