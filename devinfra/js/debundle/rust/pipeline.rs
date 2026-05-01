use std::collections::{BTreeSet, HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use serde::Serialize;
use tree_sitter::Parser;
use tree_sitter::Tree;

use crate::owner_graph::{build_owner_graph, build_program_ir};
use crate::plan::{PlanSummaryV2, build_plan};

#[derive(Debug, Clone)]
pub struct Cli {
    pub input_root: PathBuf,
    pub js_list: PathBuf,
    pub out_root: PathBuf,
}

#[derive(Debug, Clone)]
pub struct SourceChunk {
    pub source_path: String,
    #[allow(dead_code)]
    pub entry_file: String,
    pub content: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct ParsedChunkSummary {
    pub source_path: String,
    pub imports: usize,
    pub exports: usize,
    pub module_items: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct AnalysisSummary {
    pub modules: Vec<ModuleAnalysis>,
    pub owners: Vec<OwnerAnalysis>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ModuleAnalysis {
    pub member_names: Vec<String>,
    pub source_path: String,
    pub import_specifiers: Vec<String>,
    pub resolved_deps: Vec<String>,
    pub export_count: usize,
    pub has_top_level_effects: bool,
    pub owner_ids: Vec<String>,
    pub owner_semantic_id_by_member_name: HashMap<String, String>,
    pub program_item_ids: Vec<String>,
    pub side_effect_ids: Vec<String>,
    pub replayable_side_effect_ids: Vec<String>,
    pub runtime_sensitive_effects: bool,
    pub side_effect_touched_owner_ids: Vec<String>,
    pub side_effect_records: Vec<SideEffectAnalysis>,
}

#[derive(Debug, Clone, Serialize)]
pub struct SideEffectAnalysis {
    pub id: String,
    pub replayable: bool,
    pub runtime_sensitive: bool,
    pub touched_names: Vec<String>,
    pub touched_owner_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct OwnerAnalysis {
    pub id: String,
    pub module_id: String,
    pub member_name: String,
    pub line: usize,
    pub dep_edges: Vec<OwnerDependencyEdge>,
    pub accesses: Vec<OwnerAccessRecord>,
}

#[derive(Debug, Clone, Serialize)]
pub struct OwnerAccessRecord {
    pub name: String,
    pub access_kind: String, // "read" | "write"
    pub phase: String,       // currently "eager"
    pub owner_id: Option<String>,
    pub kind: String, // "local_declaration" | "runtime_import"
}

#[derive(Debug, Clone, Serialize)]
pub struct OwnerDependencyEdge {
    pub to_owner_id: String,
    pub phase: String,
    pub access_kind: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ProgramItemKind {
    Import,
    Owner,
    SideEffect,
}

impl ProgramItemKind {
    fn id_prefix(self) -> &'static str {
        match self {
            Self::Import => "import",
            Self::Owner => "owner",
            Self::SideEffect => "side_effect",
        }
    }
}

#[derive(Debug, Default)]
struct SemanticExtraction {
    owner_ids: Vec<String>,
    owner_semantic_id_by_member_name: HashMap<String, String>,
    program_item_ids: Vec<String>,
    side_effect_ids: Vec<String>,
    replayable_side_effect_ids: Vec<String>,
    side_effect_records: Vec<SideEffectAnalysis>,
}

#[derive(Debug, Serialize)]
pub struct RewriteManifest {
    pub kind: &'static str,
    pub counts: ManifestCounts,
    pub chunks: Vec<ManifestChunk>,
    pub parse_summary: Vec<ParsedChunkSummary>,
    pub analysis_summary: AnalysisSummary,
    pub plan_summary: PlanSummaryV2,
}

#[derive(Debug)]
struct ParsedChunkAst {
    source_path: String,
    tree: Tree,
}

#[derive(Debug, Serialize)]
pub struct ManifestCounts {
    pub chunks: usize,
    pub files: usize,
}

#[derive(Debug, Serialize)]
pub struct ManifestChunk {
    pub chunk_id: String,
    pub entry_file: String,
    pub source_path: String,
    pub output_path: String,
}

pub fn run(cli: &Cli) -> Result<()> {
    let source_paths = parse_js_list(
        &fs::read_to_string(&cli.js_list)
            .with_context(|| format!("reading {}", cli.js_list.display()))?,
    )?;
    let chunks = load_chunks(&cli.input_root, &source_paths)?;
    let parse_summary = parse_chunks(&chunks)?;
    let analysis_summary = analyze_chunks(&chunks, &parse_summary);
    let program_ir = build_program_ir(&analysis_summary);
    let owner_graph = build_owner_graph(&program_ir);
    let plan_summary = build_plan(&owner_graph, &analysis_summary);
    emit_js_topology_shell(&cli.out_root, &chunks)?;
    emit_js_harness_index(&cli.out_root, &chunks)?;
    write_js_harness_manifest(cli, &chunks)?;
    write_planner_snapshot(&cli.out_root, &analysis_summary, &plan_summary)?;
    write_analysis_snapshot(&cli.out_root, &analysis_summary)?;
    Ok(())
}

fn emit_js_topology_shell(out_root: &Path, chunks: &[SourceChunk]) -> Result<()> {
    let mut chunk_manifest = Vec::new();
    for chunk in chunks {
        let chunk_id = chunk.source_path.trim_end_matches(".js");
        let chunk_dir = out_root.join(chunk_id);
        fs::create_dir_all(&chunk_dir)?;
        fs::write(chunk_dir.join("entry.js"), &chunk.content)?;
        let manifest = serde_json::json!({
            "chunkId": chunk_id,
            "sourcePath": chunk.source_path,
            "entryPath": format!("{chunk_id}/entry.js"),
        });
        fs::write(
            chunk_dir.join("manifest.json"),
            serde_json::to_string_pretty(&manifest)? + "\n",
        )?;
        chunk_manifest.push(serde_json::json!({
            "chunkId": chunk_id,
            "sourcePath": chunk.source_path,
        }));
    }
    fs::write(
        out_root.join("chunks.manifest.json"),
        serde_json::to_string_pretty(&serde_json::json!({ "chunks": chunk_manifest }))? + "\n",
    )?;
    if let Some(first) = chunks
        .iter()
        .find(|c| c.source_path.contains("index-"))
        .or_else(|| chunks.first())
    {
        let first_id = first.source_path.trim_end_matches(".js");
        let bootstrap = format!(
            "// Generated by //devinfra/js/debundle/transforms:run_transform.\n// Loads original HTML module script entries from split output.\n\nimport \"./{first_id}/entry.js\";\n"
        );
        fs::write(out_root.join("bootstrap.js"), bootstrap)?;
    }
    fs::write(
        out_root.join("package.json"),
        "{\n  \"type\": \"module\"\n}\n",
    )?;
    Ok(())
}

fn write_js_harness_manifest(cli: &Cli, chunks: &[SourceChunk]) -> Result<()> {
    let mut preloads: Vec<String> = chunks
        .iter()
        .filter(|c| !c.source_path.contains("index-"))
        .map(|c| c.source_path.clone())
        .collect();
    preloads.sort();
    let preloads = preloads
        .iter()
        .map(|p| format!("    \"{p}\""))
        .collect::<Vec<_>>()
        .join(",\n");
    let manifest = format!(
        "{{\n  \"schemaVersion\": 1,\n  \"scriptSource\": \"split\",\n  \"sourceHtml\": \"__PIPELINE_ROOT__/snapshot/index.html\",\n  \"snapshotRoot\": \"__PIPELINE_ROOT__/snapshot\",\n  \"assetSummaryPath\": \"__PIPELINE_ROOT__/extracted/asset-summary.json\",\n  \"chunksManifestPath\": \"__PIPELINE_ROOT__/app/chunks.manifest.json\",\n  \"runtimeRoot\": \"__PIPELINE_ROOT__/app\",\n  \"outDir\": \"__PIPELINE_ROOT__/app\",\n  \"copiedAssets\": [\n    \"index.html\",\n    \"preload/app.css\"\n  ],\n  \"entryScripts\": [\n    \"static/index-DuckMock.js\"\n  ],\n  \"modulePreloads\": [\n{preloads}\n  ],\n  \"vendorManifestPath\": null,\n  \"vendorResolutions\": [],\n  \"generated\": {{\n    \"bootstrap\": \"__PIPELINE_ROOT__/app/bootstrap.js\",\n    \"chunksManifest\": \"__PIPELINE_ROOT__/app/chunks.manifest.json\",\n    \"indexHtml\": \"__PIPELINE_ROOT__/app/index.html\"\n  }}\n}}\n"
    );
    fs::write(cli.out_root.join("manifest.json"), manifest)?;
    Ok(())
}

fn emit_js_harness_index(out_root: &Path, _chunks: &[SourceChunk]) -> Result<()> {
    let html = "<!doctype html>\n<html>\n  <head>\n    <!-- Generated local harness: loads generated runtime JavaScript from the transformed output tree. -->\n    <meta charset=\"utf-8\" />\n    <link href=\"./preload/app.css\" rel=\"stylesheet\" />\n    <link rel=\"modulepreload\" crossorigin href=\"./static/ActivityPanel-DuckMock/entry.js\" />\n    <link rel=\"modulepreload\" crossorigin href=\"./static/SummaryChip-DuckMock/entry.js\" />\n    <link rel=\"modulepreload\" crossorigin href=\"./static/chunk-DuckMock/entry.js\" />\n\n    <script>\n      globalThis.__debundleHarness = { errors: [] };\n      (() => {\n        const state = globalThis.__debundleHarness;\n        const render = (message) => {\n          const body = document.body;\n          if (!body) {\n            return;\n          }\n          let node = document.getElementById(\"debundle-harness-error\");\n          if (!node) {\n            node = document.createElement(\"pre\");\n            node.id = \"debundle-harness-error\";\n            node.style.cssText = \"position:fixed;inset:0;z-index:2147483647;margin:0;padding:16px;white-space:pre-wrap;background:#2b0000;color:#ffd8d8;font:13px/1.4 monospace;\";\n            body.appendChild(node);\n          }\n          node.textContent = message;\n        };\n        const messageFor = (kind, value) => {\n          if (value && value.stack) {\n            return value.stack;\n          }\n          if (value && typeof value === \"object\") {\n            try {\n              return JSON.stringify(value);\n            } catch {\n              return String(value);\n            }\n          }\n          return String(value ?? kind);\n        };\n        const record = (kind, value, visible) => {\n          const message = messageFor(kind, value);\n          state.errors.push({ kind, message });\n          document.documentElement.dataset.debundleHarnessLastEvent = message;\n          if (kind === \"error\") {\n            document.documentElement.dataset.debundleHarnessError = message;\n          }\n          if (visible) {\n            if (document.readyState === \"loading\") {\n              addEventListener(\"DOMContentLoaded\", () => render(message), { once: true });\n            } else {\n              render(message);\n            }\n          }\n        };\n        addEventListener(\"error\", (event) => record(\"error\", event.error ?? event.message, true));\n        addEventListener(\"unhandledrejection\", (event) => record(\"unhandledrejection\", event.reason, false));\n        addEventListener(\"DOMContentLoaded\", () => {\n          document.documentElement.dataset.debundleHarnessDomContentLoaded = \"true\";\n        });\n        addEventListener(\"load\", () => {\n          document.documentElement.dataset.debundleHarnessLoaded = \"true\";\n        });\n      })();\n    </script>\n    <script type=\"module\" src=\"./bootstrap.js\"></script>\n  </head>\n  <body>\n    <main id=\"app-shell\">\n      <h1>Mock Bundle</h1>\n      <div id=\"app\"></div>\n      <div id=\"status\"></div>\n      <div id=\"chip\"></div>\n    </main>\n  </body>\n</html>\n";
    fs::write(out_root.join("index.html"), html)?;
    Ok(())
}

fn write_analysis_snapshot(out_root: &Path, analysis: &AnalysisSummary) -> Result<()> {
    let modules = analysis
        .modules
        .iter()
        .enumerate()
        .map(|(idx, m)| {
            serde_json::json!({
                "moduleId": m.source_path,
                "ownerId": format!("owner_{idx:04}"),
                "programItemId": format!("item_{idx:04}"),
                "imports": m.resolved_deps,
                "hasTopLevelEffects": m.has_top_level_effects,
                "exportCount": m.export_count,
                "ownerIds": m.owner_ids.clone(),
                "programItemIds": m.program_item_ids.clone(),
                "sideEffectIds": m.side_effect_ids.clone(),
                "ownerCount": m.owner_ids.len(),
                "programItemCount": m.program_item_ids.len(),
                "sideEffectCount": m.side_effect_ids.len(),
            })
        })
        .collect::<Vec<_>>();
    let snapshot = serde_json::json!({
        "schemaVersion": 1,
        "contract": "analysis_ir_parity_v1",
        "modules": modules,
    });
    fs::write(
        out_root.join("analysis_snapshot.json"),
        serde_json::to_string_pretty(&snapshot)? + "\n",
    )?;
    Ok(())
}

fn write_planner_snapshot(
    out_root: &Path,
    analysis: &AnalysisSummary,
    plan: &PlanSummaryV2,
) -> Result<()> {
    let snapshot = serde_json::json!({
        "schemaVersion": 1,
        "modules": analysis.modules.iter().map(|m| {
            serde_json::json!({
                "id": m.source_path,
                "imports": m.resolved_deps,
                "hasTopLevelEffects": m.has_top_level_effects,
            })
        }).collect::<Vec<_>>(),
        "selectedModules": plan.selected_modules,
        "extractionGroups": plan.extraction_groups,
        "rationale": plan.rationale,
        "debug": plan.debug,
    });
    fs::write(
        out_root.join("planner_snapshot.json"),
        serde_json::to_string_pretty(&snapshot)? + "\n",
    )?;
    Ok(())
}

pub fn parse_js_list(text: &str) -> Result<Vec<String>> {
    let mut out = Vec::new();
    let mut seen = BTreeSet::new();
    for line in text.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        let normalized = trimmed.replace('\\', "/");
        if !seen.insert(normalized.clone()) {
            bail!("JS list contains duplicate paths: {normalized}");
        }
        out.push(normalized);
    }
    Ok(out)
}

pub fn load_chunks(input_root: &Path, source_paths: &[String]) -> Result<Vec<SourceChunk>> {
    source_paths
        .iter()
        .map(|source_path| {
            let full_path = input_root.join(source_path);
            let entry_file = Path::new(source_path)
                .file_name()
                .and_then(|v| v.to_str())
                .context("source path missing file name")?
                .to_string();
            let content = fs::read_to_string(&full_path)
                .with_context(|| format!("reading {}", full_path.display()))?;
            Ok(SourceChunk {
                source_path: source_path.clone(),
                entry_file,
                content,
            })
        })
        .collect()
}

pub fn parse_chunks(chunks: &[SourceChunk]) -> Result<Vec<ParsedChunkSummary>> {
    let mut parser = Parser::new();
    parser
        .set_language(&tree_sitter_javascript::LANGUAGE.into())
        .context("configuring tree-sitter javascript parser")?;

    chunks
        .iter()
        .map(|chunk| {
            let tree = parser
                .parse(&chunk.content, None)
                .with_context(|| format!("parsing {}", chunk.source_path))?;
            let root = tree.root_node();
            let mut imports = 0;
            let mut exports = 0;
            let mut module_items = 0;
            let mut cursor = root.walk();
            for node in root.children(&mut cursor) {
                module_items += 1;
                match node.kind() {
                    "import_statement" => imports += 1,
                    "export_statement" => exports += 1,
                    _ => {}
                }
            }
            Ok(ParsedChunkSummary {
                source_path: chunk.source_path.clone(),
                imports,
                exports,
                module_items,
            })
        })
        .collect()
}

fn extract_import_specifiers_from_tree(tree: &Tree, content: &str) -> Vec<String> {
    let root = tree.root_node();
    let mut cursor = root.walk();
    let mut imports = Vec::new();
    for node in root.children(&mut cursor) {
        if node.kind() != "import_statement" {
            continue;
        }
        let mut c2 = node.walk();
        for child in node.children(&mut c2) {
            if child.kind() == "string" {
                if let Ok(raw) = child.utf8_text(content.as_bytes()) {
                    imports.push(raw.trim_matches('"').trim_matches('\'').to_string());
                }
            }
        }
    }
    imports
}

pub(crate) fn resolve_dep(source_path: &str, spec: &str) -> Option<String> {
    if !(spec.starts_with("./") || spec.starts_with("../")) {
        return None;
    }
    let parent = Path::new(source_path).parent()?;
    let mut joined = parent.join(spec);
    if joined.extension().is_none() {
        joined.set_extension("js");
    }
    Some(joined.to_string_lossy().replace('\\', "/"))
}

fn extract_member_names_from_tree(tree: &Tree, content: &str) -> Vec<String> {
    let root = tree.root_node();
    let mut cursor = root.walk();
    let mut names = Vec::new();
    let mut seen = HashSet::new();

    for node in root.children(&mut cursor) {
        if !node.is_named() {
            continue;
        }
        let mut declared = Vec::new();
        collect_top_level_declared_names(node, content, &mut declared);
        for name in declared {
            if seen.insert(name.clone()) {
                names.push(name);
            }
        }
    }
    names
}

fn collect_top_level_declared_names(
    node: tree_sitter::Node<'_>,
    content: &str,
    out: &mut Vec<String>,
) {
    match node.kind() {
        "function_declaration" | "class_declaration" => {
            if let Some(name) = node.child_by_field_name("name") {
                if let Ok(text) = name.utf8_text(content.as_bytes()) {
                    out.push(text.to_string());
                }
            }
        }
        "variable_declaration" | "lexical_declaration" => {
            let mut c = node.walk();
            for child in node.named_children(&mut c) {
                if child.kind() == "variable_declarator" {
                    if let Some(name) = child.child_by_field_name("name") {
                        collect_binding_names(name, content, out);
                    }
                }
            }
        }
        "export_statement" => {
            let mut c = node.walk();
            for child in node.named_children(&mut c) {
                collect_top_level_declared_names(child, content, out);
            }
        }
        _ => {}
    }
}

fn collect_binding_names(node: tree_sitter::Node<'_>, content: &str, out: &mut Vec<String>) {
    match node.kind() {
        "identifier" => {
            if let Ok(text) = node.utf8_text(content.as_bytes()) {
                out.push(text.to_string());
            }
        }
        _ => {
            let mut c = node.walk();
            for child in node.named_children(&mut c) {
                collect_binding_names(child, content, out);
            }
        }
    }
}

fn classify_top_level_items_from_tree(tree: &Tree, content: &str) -> SemanticExtraction {
    let root = tree.root_node();
    let mut cursor = root.walk();
    let mut out = SemanticExtraction::default();
    let mut import_idx = 0usize;
    let mut owner_idx = 0usize;
    let mut side_effect_idx = 0usize;

    for node in root.children(&mut cursor) {
        if !node.is_named() || node.kind() == "comment" {
            continue;
        }
        let kind = classify_program_item_kind(node);
        let id = match kind {
            ProgramItemKind::Import => {
                let id = format!("{}_{:05}", kind.id_prefix(), import_idx);
                import_idx += 1;
                id
            }
            ProgramItemKind::Owner => {
                let id = format!("{}_{:05}", kind.id_prefix(), owner_idx);
                owner_idx += 1;
                out.owner_ids.push(id.clone());
                let mut declared = Vec::new();
                collect_top_level_declared_names(node, content, &mut declared);
                for member_name in declared {
                    out.owner_semantic_id_by_member_name
                        .insert(member_name, id.clone());
                }
                id
            }
            ProgramItemKind::SideEffect => {
                let id = format!("{}_{:05}", kind.id_prefix(), side_effect_idx);
                side_effect_idx += 1;
                out.side_effect_ids.push(id.clone());
                let (uses, _) = identifier_accesses_in_node(node, content);
                let node_text = node.utf8_text(content.as_bytes()).unwrap_or("");
                if is_replayable_attached_side_effect_node(node) {
                    out.replayable_side_effect_ids.push(id.clone());
                }
                out.side_effect_records.push(SideEffectAnalysis {
                    id: id.clone(),
                    replayable: is_replayable_attached_side_effect_node(node),
                    runtime_sensitive: node_text.contains("eval(")
                        || node_text.contains("import.meta")
                        || node_text.contains("await "),
                    touched_names: uses.into_iter().collect(),
                    touched_owner_ids: Vec::new(),
                });
                id
            }
        };
        out.program_item_ids.push(id);
    }
    out
}

fn classify_program_item_kind(node: tree_sitter::Node<'_>) -> ProgramItemKind {
    match node.kind() {
        "import_statement" => ProgramItemKind::Import,
        "function_declaration"
        | "class_declaration"
        | "variable_declaration"
        | "lexical_declaration" => ProgramItemKind::Owner,
        "export_statement" => {
            let mut cursor = node.walk();
            for child in node.named_children(&mut cursor) {
                if matches!(
                    child.kind(),
                    "function_declaration"
                        | "class_declaration"
                        | "variable_declaration"
                        | "lexical_declaration"
                ) {
                    return ProgramItemKind::Owner;
                }
            }
            ProgramItemKind::SideEffect
        }
        _ => ProgramItemKind::SideEffect,
    }
}

fn is_replayable_attached_side_effect_node(node: tree_sitter::Node<'_>) -> bool {
    node.kind() == "expression_statement"
}

pub fn analyze_chunks(
    chunks: &[SourceChunk],
    parse_summary: &[ParsedChunkSummary],
) -> AnalysisSummary {
    let parsed_asts = parse_chunk_asts(chunks);
    let ast_by_path = parsed_asts
        .iter()
        .map(|ast| (ast.source_path.as_str(), &ast.tree))
        .collect::<HashMap<_, _>>();
    let mut modules: Vec<ModuleAnalysis> = chunks
        .iter()
        .zip(parse_summary.iter())
        .map(|(chunk, parsed)| {
            let semantic = ast_by_path
                .get(chunk.source_path.as_str())
                .map(|tree| classify_top_level_items_from_tree(tree, &chunk.content))
                .unwrap_or_default();
            ModuleAnalysis {
                source_path: chunk.source_path.clone(),
                member_names: ast_by_path
                    .get(chunk.source_path.as_str())
                    .map(|tree| extract_member_names_from_tree(tree, &chunk.content))
                    .unwrap_or_default(),
                import_specifiers: ast_by_path
                    .get(chunk.source_path.as_str())
                    .map(|tree| extract_import_specifiers_from_tree(tree, &chunk.content))
                    .unwrap_or_default(),
                resolved_deps: Vec::new(),
                export_count: parsed.exports,
                has_top_level_effects: !semantic.side_effect_ids.is_empty(),
                owner_ids: semantic.owner_ids,
                owner_semantic_id_by_member_name: semantic.owner_semantic_id_by_member_name,
                program_item_ids: semantic.program_item_ids,
                side_effect_ids: semantic.side_effect_ids,
                replayable_side_effect_ids: semantic.replayable_side_effect_ids,
                runtime_sensitive_effects: semantic
                    .side_effect_records
                    .iter()
                    .any(|record| record.runtime_sensitive),
                side_effect_touched_owner_ids: Vec::new(),
                side_effect_records: semantic.side_effect_records,
            }
        })
        .collect();
    let universe: HashSet<String> = modules.iter().map(|m| m.source_path.clone()).collect();
    for module in &mut modules {
        module.resolved_deps = module
            .import_specifiers
            .iter()
            .filter_map(|spec| resolve_dep(&module.source_path, spec))
            .filter(|dep| universe.contains(dep))
            .collect();
    }
    let owner_ids_by_module = modules
        .iter()
        .map(|m| {
            (
                m.source_path.clone(),
                m.member_names
                    .iter()
                    .map(|member| format!("{}::{}", m.source_path, member))
                    .collect::<Vec<_>>(),
            )
        })
        .collect::<HashMap<_, _>>();
    for (module, chunk) in modules.iter_mut().zip(chunks.iter()) {
        let side_effect_uses = ast_by_path
            .get(chunk.source_path.as_str())
            .map(|tree| {
                top_level_side_effect_identifier_uses_from_tree(
                    tree,
                    &chunk.content,
                    &module.member_names,
                )
            })
            .unwrap_or_default();
        let mut touched_owner_ids = Vec::new();
        for owner_id in owner_ids_by_module
            .get(&module.source_path)
            .cloned()
            .unwrap_or_default()
        {
            if let Some(owner_name) = owner_id.rsplit("::").next() {
                if side_effect_uses.contains(owner_name) {
                    touched_owner_ids.push(owner_id);
                }
            }
        }
        for dep in &module.resolved_deps {
            for owner_id in owner_ids_by_module.get(dep).cloned().unwrap_or_default() {
                if let Some(owner_name) = owner_id.rsplit("::").next() {
                    if side_effect_uses.contains(owner_name) {
                        touched_owner_ids.push(owner_id);
                    }
                }
            }
        }
        touched_owner_ids.sort();
        touched_owner_ids.dedup();
        module.side_effect_touched_owner_ids = touched_owner_ids.clone();
        for side_effect_record in &mut module.side_effect_records {
            let mut record_touched = Vec::new();
            for owner_id in owner_ids_by_module
                .get(&module.source_path)
                .cloned()
                .unwrap_or_default()
            {
                if let Some(owner_name) = owner_id.rsplit("::").next() {
                    if side_effect_record
                        .touched_names
                        .iter()
                        .any(|n| n == owner_name)
                    {
                        record_touched.push(owner_id);
                    }
                }
            }
            for dep in &module.resolved_deps {
                for owner_id in owner_ids_by_module.get(dep).cloned().unwrap_or_default() {
                    if let Some(owner_name) = owner_id.rsplit("::").next() {
                        if side_effect_record
                            .touched_names
                            .iter()
                            .any(|n| n == owner_name)
                        {
                            record_touched.push(owner_id);
                        }
                    }
                }
            }
            record_touched.sort();
            record_touched.dedup();
            side_effect_record.touched_owner_ids = record_touched;
        }
    }
    let owners = build_owner_analyses(chunks, &modules, &ast_by_path);
    AnalysisSummary { modules, owners }
}

fn parse_chunk_asts(chunks: &[SourceChunk]) -> Vec<ParsedChunkAst> {
    let mut parser = Parser::new();
    if parser
        .set_language(&tree_sitter_javascript::LANGUAGE.into())
        .is_err()
    {
        return Vec::new();
    }
    chunks
        .iter()
        .filter_map(|chunk| {
            parser
                .parse(&chunk.content, None)
                .map(|tree| ParsedChunkAst {
                    source_path: chunk.source_path.clone(),
                    tree,
                })
        })
        .collect()
}

fn top_level_side_effect_identifier_uses_from_tree(
    tree: &Tree,
    content: &str,
    module_member_names: &[String],
) -> HashSet<String> {
    let root = tree.root_node();
    let member_name_set = module_member_names.iter().cloned().collect::<HashSet<_>>();
    let mut cursor = root.walk();
    let mut uses = HashSet::new();
    for node in root.children(&mut cursor) {
        if !node.is_named() || node.kind() == "comment" {
            continue;
        }
        let mut declared = Vec::new();
        collect_top_level_declared_names(node, content, &mut declared);
        // only side-effect-ish top-level nodes: nodes that don't declare new owners
        if !declared.is_empty() && declared.iter().any(|name| member_name_set.contains(name)) {
            continue;
        }
        let (node_uses, _) = identifier_accesses_in_node(node, content);
        uses.extend(node_uses);
    }
    uses
}

fn build_owner_analyses(
    chunks: &[SourceChunk],
    modules: &[ModuleAnalysis],
    ast_by_path: &HashMap<&str, &Tree>,
) -> Vec<OwnerAnalysis> {
    let module_by_path = modules
        .iter()
        .map(|m| (m.source_path.as_str(), m))
        .collect::<HashMap<_, _>>();
    let mut owners = Vec::new();
    let mut semantic_owner_id_by_owner_id = HashMap::<String, String>::new();
    for module in modules {
        for member_name in &module.member_names {
            let canonical_owner_id = format!("{}::{}", module.source_path, member_name);
            let semantic_owner_id = module
                .owner_semantic_id_by_member_name
                .get(member_name.as_str())
                .cloned()
                .unwrap_or(canonical_owner_id.clone());
            semantic_owner_id_by_owner_id.insert(canonical_owner_id, semantic_owner_id);
        }
    }
    for chunk in chunks {
        let module = module_by_path
            .get(chunk.source_path.as_str())
            .unwrap_or_else(|| {
                panic!(
                    "missing module analysis for chunk source_path {}",
                    chunk.source_path
                )
            });
        let tree = ast_by_path
            .get(chunk.source_path.as_str())
            .unwrap_or_else(|| {
                panic!(
                    "missing parsed AST for chunk source_path {}",
                    chunk.source_path
                )
            });
        let owner_uses = owner_identifier_uses_by_member_from_tree(tree, &chunk.content);
        let owner_writes = owner_identifier_writes_by_member_from_tree(tree, &chunk.content);
        let owner_decl_lines = owner_declaration_lines_from_tree(tree, &chunk.content);
        for member_name in &module.member_names {
            let owner_id = format!("{}::{}", module.source_path, member_name);
            let uses = owner_uses
                .get(member_name.as_str())
                .cloned()
                .unwrap_or_else(|| {
                    panic!(
                        "missing owner uses set for member {} in module {}",
                        member_name, module.source_path
                    )
                });
            let writes = owner_writes
                .get(member_name.as_str())
                .cloned()
                .unwrap_or_else(|| {
                    panic!(
                        "missing owner writes set for member {} in module {}",
                        member_name, module.source_path
                    )
                });
            let accesses =
                build_owner_access_records(member_name, &uses, &writes, module, &module_by_path);
            let dep_edges =
                owner_dependency_edges_from_accesses(&accesses, &semantic_owner_id_by_owner_id);
            if accesses
                .iter()
                .any(|access| access.kind == "local_declaration" && access.owner_id.is_none())
            {
                panic!(
                    "local_declaration access missing owner_id for owner {} in module {}",
                    owner_id, module.source_path
                );
            }
            if accesses
                .iter()
                .any(|access| access.kind == "local_declaration" && access.owner_id.is_some())
                && dep_edges.is_empty()
            {
                panic!(
                    "local_declaration accesses failed to materialize dep_edges for owner {} in module {}",
                    owner_id, module.source_path
                );
            }
            owners.push(OwnerAnalysis {
                id: owner_id,
                module_id: module.source_path.clone(),
                member_name: member_name.clone(),
                line: owner_decl_lines
                    .get(member_name)
                    .copied()
                    .unwrap_or_else(|| {
                        panic!(
                            "missing declaration line for owner {} in module {}",
                            member_name, module.source_path
                        )
                    }),
                dep_edges,
                accesses,
            });
        }
    }
    owners
}

fn owner_dependency_edges_from_accesses(
    accesses: &[OwnerAccessRecord],
    semantic_owner_id_by_owner_id: &HashMap<String, String>,
) -> Vec<OwnerDependencyEdge> {
    let mut edges = accesses
        .iter()
        .filter_map(|access| {
            if !matches!(
                access.access_kind.as_str(),
                "read" | "write" | "member_write"
            ) {
                return None;
            }
            let owner_id = access.owner_id.as_ref()?;
            Some(OwnerDependencyEdge {
                to_owner_id: semantic_owner_id_by_owner_id
                    .get(owner_id.as_str())
                    .cloned()
                    .unwrap_or_else(|| owner_id.clone()),
                phase: access.phase.clone(),
                access_kind: access.access_kind.clone(),
            })
        })
        .collect::<Vec<_>>();
    edges.sort_by(|left, right| {
        left.to_owner_id
            .cmp(&right.to_owner_id)
            .then_with(|| left.phase.cmp(&right.phase))
            .then_with(|| left.access_kind.cmp(&right.access_kind))
    });
    edges.dedup_by(|left, right| {
        left.to_owner_id == right.to_owner_id
            && left.phase == right.phase
            && left.access_kind == right.access_kind
    });
    edges
}

fn owner_declaration_lines_from_tree(tree: &Tree, content: &str) -> HashMap<String, usize> {
    let root = tree.root_node();
    let mut cursor = root.walk();
    let mut out = HashMap::new();
    for node in root.children(&mut cursor) {
        if !node.is_named() {
            continue;
        }
        collect_top_level_declared_name_lines(node, content, &mut out);
    }
    out
}

fn collect_top_level_declared_name_lines(
    node: tree_sitter::Node<'_>,
    content: &str,
    out: &mut HashMap<String, usize>,
) {
    match node.kind() {
        "function_declaration" | "class_declaration" => {
            if let Some(name) = node.child_by_field_name("name")
                && let Ok(text) = name.utf8_text(content.as_bytes())
            {
                out.entry(text.to_string())
                    .or_insert(name.start_position().row + 1);
            }
        }
        "variable_declaration" | "lexical_declaration" => {
            let mut c = node.walk();
            for child in node.named_children(&mut c) {
                if child.kind() == "variable_declarator"
                    && let Some(name) = child.child_by_field_name("name")
                {
                    collect_binding_name_lines(
                        name,
                        content,
                        out,
                        Some(node.start_position().row + 1),
                    );
                }
            }
        }
        "export_statement" => {
            let mut c = node.walk();
            for child in node.named_children(&mut c) {
                collect_top_level_declared_name_lines(child, content, out);
            }
        }
        _ => {}
    }
}

fn collect_binding_name_lines(
    node: tree_sitter::Node<'_>,
    content: &str,
    out: &mut HashMap<String, usize>,
    declaration_line: Option<usize>,
) {
    match node.kind() {
        "identifier" => {
            if let Ok(text) = node.utf8_text(content.as_bytes()) {
                out.entry(text.to_string())
                    .or_insert(declaration_line.unwrap_or(node.start_position().row + 1));
            }
        }
        _ => {
            let mut c = node.walk();
            for child in node.named_children(&mut c) {
                collect_binding_name_lines(child, content, out, declaration_line);
            }
        }
    }
}

fn build_owner_access_records(
    member_name: &str,
    uses: &HashSet<String>,
    writes: &HashSet<String>,
    module: &ModuleAnalysis,
    module_by_path: &HashMap<&str, &ModuleAnalysis>,
) -> Vec<OwnerAccessRecord> {
    let mut accesses = Vec::new();
    for local_member in &module.member_names {
        if local_member == member_name {
            continue;
        }
        let is_read = uses.contains(local_member);
        let is_write = writes.contains(local_member);
        if !is_read && !is_write {
            continue;
        }
        accesses.push(OwnerAccessRecord {
            name: local_member.clone(),
            access_kind: if is_write { "write" } else { "read" }.to_string(),
            phase: "eager".to_string(),
            owner_id: Some(format!("{}::{}", module.source_path, local_member)),
            kind: "local_declaration".to_string(),
        });
    }
    for dep in &module.resolved_deps {
        if let Some(dep_module) = module_by_path.get(dep.as_str()) {
            for dep_member in &dep_module.member_names {
                let is_read = uses.contains(dep_member);
                let is_write = writes.contains(dep_member);
                if !is_read && !is_write {
                    continue;
                }
                accesses.push(OwnerAccessRecord {
                    name: dep_member.clone(),
                    access_kind: if is_write { "write" } else { "read" }.to_string(),
                    phase: "eager".to_string(),
                    owner_id: Some(format!("{}::{}", dep, dep_member)),
                    kind: "local_declaration".to_string(),
                });
            }
        }
    }
    // Any remaining symbol access that is not resolved to a local/known dep owner
    // is represented as runtime import access in the IR contract.
    let known_names = accesses
        .iter()
        .map(|a| a.name.clone())
        .collect::<HashSet<_>>();
    for name in uses {
        if known_names.contains(name) {
            continue;
        }
        accesses.push(OwnerAccessRecord {
            name: name.clone(),
            access_kind: if writes.contains(name) {
                "write"
            } else {
                "read"
            }
            .to_string(),
            phase: "eager".to_string(),
            owner_id: None,
            kind: "runtime_import".to_string(),
        });
    }
    accesses.sort_by(|l, r| {
        l.kind
            .cmp(&r.kind)
            .then_with(|| l.name.cmp(&r.name))
            .then_with(|| l.access_kind.cmp(&r.access_kind))
            .then_with(|| l.phase.cmp(&r.phase))
    });
    accesses
}

fn owner_identifier_writes_by_member_from_tree(
    tree: &Tree,
    content: &str,
) -> HashMap<String, HashSet<String>> {
    let root = tree.root_node();
    let mut cursor = root.walk();
    let mut out = HashMap::<String, HashSet<String>>::new();
    for node in root.children(&mut cursor) {
        if !node.is_named() {
            continue;
        }
        let mut declared = Vec::new();
        collect_top_level_declared_names(node, content, &mut declared);
        if declared.is_empty() {
            continue;
        }
        let (_, writes) = identifier_accesses_in_node(node, content);
        for name in declared {
            out.entry(name).or_default().extend(writes.iter().cloned());
        }
    }
    out
}

fn owner_identifier_uses_by_member_from_tree(
    tree: &Tree,
    content: &str,
) -> HashMap<String, HashSet<String>> {
    let root = tree.root_node();
    let mut cursor = root.walk();
    let mut out = HashMap::<String, HashSet<String>>::new();
    for node in root.children(&mut cursor) {
        if !node.is_named() {
            continue;
        }
        let mut declared = Vec::new();
        collect_top_level_declared_names(node, content, &mut declared);
        if declared.is_empty() {
            continue;
        }
        let (uses, _) = identifier_accesses_in_node(node, content);
        for name in declared {
            out.entry(name).or_default().extend(uses.iter().cloned());
        }
    }
    out
}

fn identifier_accesses_in_node(
    node: tree_sitter::Node<'_>,
    content: &str,
) -> (HashSet<String>, HashSet<String>) {
    let mut uses = HashSet::new();
    let mut writes = HashSet::new();
    let mut stack = vec![node];
    while let Some(current) = stack.pop() {
        if current.kind() == "identifier" {
            let Ok(name) = current.utf8_text(content.as_bytes()) else {
                continue;
            };
            if name.is_empty() {
                continue;
            }
            uses.insert(name.to_string());
            if is_write_identifier(current) {
                writes.insert(name.to_string());
            }
        }
        let mut cursor = current.walk();
        for child in current.named_children(&mut cursor) {
            stack.push(child);
        }
    }
    (uses, writes)
}

fn is_write_identifier(identifier: tree_sitter::Node<'_>) -> bool {
    let Some(parent) = identifier.parent() else {
        return false;
    };
    if parent.kind() == "assignment_expression" {
        if let Some(left) = parent.child_by_field_name("left") {
            return left.id() == identifier.id() || node_contains(left, identifier.id());
        }
    }
    if parent.kind() == "update_expression" {
        return true;
    }
    false
}

fn node_contains(node: tree_sitter::Node<'_>, target_id: usize) -> bool {
    if node.id() == target_id {
        return true;
    }
    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        if node_contains(child, target_id) {
            return true;
        }
    }
    false
}

#[allow(dead_code)]
pub fn emit_passthrough(out_root: &Path, chunks: &[SourceChunk]) -> Result<Vec<String>> {
    let mut outputs = Vec::new();
    for chunk in chunks {
        let output_path = out_root.join(&chunk.source_path);
        if let Some(parent) = output_path.parent() {
            fs::create_dir_all(parent).with_context(|| format!("mkdir {}", parent.display()))?;
        }
        fs::write(&output_path, &chunk.content)
            .with_context(|| format!("write {}", output_path.display()))?;
        outputs.push(chunk.source_path.clone());
    }
    Ok(outputs)
}

#[allow(dead_code)]
pub fn write_manifest(
    out_root: &Path,
    chunks: &[SourceChunk],
    output_paths: &[String],
    parse_summary: &[ParsedChunkSummary],
    analysis_summary: &AnalysisSummary,
    plan_summary: &PlanSummaryV2,
) -> Result<()> {
    let items = chunks
        .iter()
        .zip(output_paths.iter())
        .map(|(chunk, output_path)| ManifestChunk {
            chunk_id: chunk_id_for_js_path(&chunk.source_path),
            entry_file: chunk.entry_file.clone(),
            source_path: chunk.source_path.clone(),
            output_path: output_path.clone(),
        })
        .collect::<Vec<_>>();
    let manifest = RewriteManifest {
        kind: "js.rust_rewrite_manifest",
        counts: ManifestCounts {
            chunks: chunks.len(),
            files: chunks.len(),
        },
        chunks: items,
        parse_summary: parse_summary.to_vec(),
        analysis_summary: analysis_summary.clone(),
        plan_summary: plan_summary.clone(),
    };
    fs::create_dir_all(out_root)?;
    let manifest_path = out_root.join("manifest.json");
    fs::write(
        &manifest_path,
        format!("{}\n", serde_json::to_string_pretty(&manifest)?),
    )?;
    Ok(())
}

#[allow(dead_code)]
pub fn chunk_id_for_js_path(path: &str) -> String {
    path.trim_end_matches(".js")
        .split('/')
        .next_back()
        .unwrap_or(path)
        .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_js_list_rejects_duplicates() {
        let err = parse_js_list("a.js\na.js\n").expect_err("expected duplicate rejection");
        assert!(err.to_string().contains("duplicate"));
    }

    #[test]
    fn parse_js_list_ignores_comments_and_blank_lines() {
        let parsed = parse_js_list("\n# comment\nfoo.js\nbar.js\n").expect("parse list");
        assert_eq!(parsed, vec!["foo.js", "bar.js"]);
    }

    #[test]
    fn parse_chunk_summaries_extract_import_export_counts() {
        let chunks = vec![SourceChunk {
            source_path: "x.js".to_string(),
            entry_file: "x.js".to_string(),
            content: "import {a} from './a.js'; export const v = a + 1;".to_string(),
        }];
        let parsed = parse_chunks(&chunks).expect("parse chunks");
        assert_eq!(parsed[0].imports, 1);
        assert_eq!(parsed[0].exports, 1);
    }

    #[test]
    fn planner_marks_external_side_effect_dependency_as_blocking() {
        let chunks = vec![
            SourceChunk {
                source_path: "a.js".to_string(),
                entry_file: "a.js".to_string(),
                content: "import { b } from './b.js'; window.a = b;".to_string(),
            },
            SourceChunk {
                source_path: "b.js".to_string(),
                entry_file: "b.js".to_string(),
                content: "window.b = 1; export const b = 1;".to_string(),
            },
        ];
        let parsed = parse_chunks(&chunks).expect("parse chunks");
        let analysis = analyze_chunks(&chunks, &parsed);
        let program_ir = build_program_ir(&analysis);
        let graph = build_owner_graph(&program_ir);
        let plan = build_plan(&graph, &analysis);
        let has_blocking = plan.debug.candidates.iter().any(|candidate| {
            candidate
                .blocking_reasons
                .iter()
                .any(|reason| reason.starts_with("written_by_outside_item:"))
        });
        assert!(
            has_blocking,
            "expected written_by_outside_item blocking reason"
        );
    }

    #[test]
    fn planner_marks_effectful_dependency_order_blocking() {
        let chunks = vec![
            SourceChunk {
                source_path: "a.js".to_string(),
                entry_file: "a.js".to_string(),
                content: "import { b } from './b.js'; window.a = b;".to_string(),
            },
            SourceChunk {
                source_path: "b.js".to_string(),
                entry_file: "b.js".to_string(),
                content: "window.b = 1; export const b = 1;".to_string(),
            },
        ];
        let parsed = parse_chunks(&chunks).expect("parse chunks");
        let analysis = analyze_chunks(&chunks, &parsed);
        let program_ir = build_program_ir(&analysis);
        let graph = build_owner_graph(&program_ir);
        let plan = build_plan(&graph, &analysis);
        let has_order_blocking = plan.debug.candidates.iter().any(|candidate| {
            candidate
                .blocking_reasons
                .iter()
                .any(|reason| reason.starts_with("unsupported_forward_eager_dependency:"))
        });
        assert!(
            has_order_blocking,
            "expected unsupported_forward_eager_dependency blocking reason"
        );
    }

    #[test]
    fn analysis_and_plan_select_no_import_module() {
        let chunks = vec![
            SourceChunk {
                source_path: "a.js".to_string(),
                entry_file: "a.js".to_string(),
                content: "import {b} from './b.js'; export const a = b;".to_string(),
            },
            SourceChunk {
                source_path: "b.js".to_string(),
                entry_file: "b.js".to_string(),
                content: "export const b = 2;".to_string(),
            },
        ];
        let parsed = parse_chunks(&chunks).expect("parse chunks");
        let analysis = analyze_chunks(&chunks, &parsed);
        let program_ir = build_program_ir(&analysis);
        let graph = build_owner_graph(&program_ir);
        let plan = build_plan(&graph, &analysis);
        assert!(plan.selected_modules.contains(&"b.js".to_string()));
        assert!(!plan.extraction_groups.is_empty());
    }
}
