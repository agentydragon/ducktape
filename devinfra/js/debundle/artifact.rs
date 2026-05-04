use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use serde::Serialize;

use js_ast::{ParsedJsModule, emit_js_module, parse_js_module};

pub const CANONICAL_CHUNK_ENTRY_FILE: &str = "entry.js";

#[derive(Default)]
pub struct JsPipelineArtifact {
    pub chunk_order: Vec<String>,
    pub chunks: BTreeMap<String, JsChunk>,
    pub root_manifest: Option<ArtifactManifest>,
    pub chunk_manifests: BTreeMap<String, ChunkManifest>,
}

pub struct JsChunk {
    pub entry_file: String,
    pub files: BTreeMap<String, JsFile>,
    pub metadata: ChunkMetadata,
}

pub struct JsFile {
    pub path: String,
    pub content: Option<String>,
    pub ast: Option<ParsedJsModule>,
    pub header_lines: Vec<String>,
    pub metadata: FileMetadata,
}

#[derive(Debug, Clone, Default)]
#[allow(dead_code)]
pub struct ChunkMetadata {
    pub source_path: Option<String>,
    pub module_extraction_state: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Default)]
pub struct FileMetadata {
    pub chunk_id: Option<String>,
    pub chunk_file: Option<String>,
    pub role: Option<String>,
    pub source_path: Option<String>,
    pub generated_stage: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct LoadedJsChunksManifest {
    pub kind: &'static str,
    pub counts: LoadedCounts,
    pub chunks: Vec<LoadedChunkRecord>,
    #[serde(rename = "jsFiles")]
    pub js_files: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct LoadedCounts {
    pub chunks: usize,
    pub files: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct LoadedChunkRecord {
    #[serde(rename = "chunkId")]
    pub chunk_id: String,
    #[serde(rename = "entryFile")]
    pub entry_file: String,
    #[serde(rename = "sourcePath")]
    pub source_path: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct ComputeJsAstsManifest {
    pub kind: &'static str,
    pub counts: ComputeJsAstsCounts,
}

#[derive(Debug, Clone, Serialize)]
pub struct ComputeJsAstsCounts {
    pub parsed: usize,
    pub files: usize,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ArtifactManifest {
    pub schema_version: u32,
    pub counts: ArtifactCounts,
    pub chunks: Vec<ArtifactChunkRecord>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub logical_modules: Option<RootLogicalModulesSummary>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub selected_module_lowerings: Option<Vec<SelectedModuleLowering>>,
    /// Path (manifest-relative) to the scrambled-identifier frequency
    /// queue side output, when this manifest was produced by a stage
    /// that emits to a writable directory (e.g. `write_js_tree`).
    /// `None` for early-pipeline manifests that never see a final
    /// output directory.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub scrambled_identifier_frequencies: Option<String>,
    #[serde(flatten)]
    pub extra: BTreeMap<String, serde_json::Value>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ArtifactCounts {
    pub chunks: usize,
    pub kept_top_level_declaration_owners: usize,
    pub top_level_side_effects: usize,
    pub export_aliases: usize,
    pub unresolved_exports: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub selected_module_lowerings: Option<usize>,
    #[serde(flatten)]
    pub extra: BTreeMap<String, serde_json::Value>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RootLogicalModulesSummary {
    pub chunk_count: usize,
    pub module_count: usize,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ChunkLogicalModulesSummary {
    pub count: usize,
    pub module_ids: Vec<String>,
    pub target_dir: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SelectedModuleLowering {
    pub chunk_id: String,
    pub exported_names: Vec<String>,
    pub file: String,
    pub id: String,
    pub operation: &'static str,
    pub owner_ids: Vec<String>,
    pub target_file: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ArtifactChunkRecord {
    pub chunk_id: String,
    pub source_path: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ChunkManifest {
    pub schema_version: u32,
    pub chunk_id: String,
    pub source_path: String,
    pub parser: ParserOptionsRecord,
    pub entry_file: String,
    pub counts: ChunkCounts,
    pub files: Vec<ChunkFileRecord>,
    pub imports: Vec<ImportRecord>,
    pub export_aliases: Vec<ExportAliasRecord>,
    pub unresolved_exports: Vec<ExportAliasRecord>,
    pub kept_top_level_declarations: Vec<KeptTopLevelDeclarationRecord>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub logical_modules: Option<ChunkLogicalModulesSummary>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub selected_module_lowerings: Option<Vec<SelectedModuleLowering>>,
    #[serde(flatten)]
    pub extra: BTreeMap<String, serde_json::Value>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ParserOptionsRecord {
    pub allow_undeclared_exports: bool,
    pub plugins: Vec<&'static str>,
    pub source_type: &'static str,
}

impl Default for ParserOptionsRecord {
    fn default() -> Self {
        Self {
            allow_undeclared_exports: true,
            plugins: vec!["jsx", "typescript", "importAssertions", "topLevelAwait"],
            source_type: "module",
        }
    }
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ChunkCounts {
    pub dynamic_imports: usize,
    pub export_aliases: usize,
    pub import_declarations: usize,
    pub kept_top_level_declaration_owners: usize,
    pub top_level_bindings: usize,
    pub top_level_declaration_owners: usize,
    pub top_level_side_effects: usize,
    pub unresolved_exports: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChunkFileRecord {
    pub file: String,
    pub role: &'static str,
}

#[derive(Debug, Clone, Serialize)]
pub struct ImportRecord {
    pub id: String,
    pub line: Option<usize>,
    pub source: String,
    pub specifiers: Vec<ImportSpecifierRecord>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ImportSpecifierRecord {
    pub kind: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub imported: Option<String>,
    pub local: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ExportAliasRecord {
    pub exported: String,
    pub line: Option<usize>,
    pub local: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KeptTopLevelDeclarationRecord {
    pub id: String,
    pub line: Option<usize>,
    pub names: Vec<String>,
    pub kind: TopLevelDeclarationKind,
    pub unsafe_reason: &'static str,
}

/// The three top-level declaration variants we anchor extraction on.
/// Mirrors the SWC `Decl` arms where `analyze_program_shallow` produces
/// an `OwnerRecord`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum TopLevelDeclarationKind {
    Function,
    Class,
    Variable,
}

impl JsPipelineArtifact {
    pub fn list_chunk_ids(&self) -> Vec<String> {
        if self.chunk_order.is_empty() {
            self.chunks.keys().cloned().collect()
        } else {
            self.chunk_order.clone()
        }
    }

    pub fn list_js_file_keys(&self) -> Vec<(String, String)> {
        self.list_chunk_ids()
            .into_iter()
            .flat_map(|chunk_id| {
                self.chunks
                    .get(&chunk_id)
                    .map(|chunk| {
                        chunk
                            .files
                            .keys()
                            .map(|file| (chunk_id.clone(), file.clone()))
                            .collect::<Vec<_>>()
                    })
                    .unwrap_or_default()
            })
            .collect()
    }

    pub fn chunk_source_path(&self, chunk_id: &str) -> Option<String> {
        self.chunk_manifests
            .get(chunk_id)
            .map(|manifest| manifest.source_path.clone())
            .or_else(|| {
                self.chunks
                    .get(chunk_id)
                    .and_then(|chunk| chunk.metadata.source_path.clone())
            })
            .or_else(|| Some(format!("{chunk_id}.js")))
    }

    pub fn source_chunk_index(&self) -> Result<BTreeMap<String, String>> {
        let mut out = BTreeMap::new();
        for chunk_id in self.list_chunk_ids() {
            let Some(source_path) = self.chunk_source_path(&chunk_id) else {
                continue;
            };
            if let Some(existing) = out.insert(source_path.clone(), chunk_id.clone()) {
                bail!("Duplicate chunk sourcePath {source_path}: {existing} and {chunk_id}");
            }
        }
        Ok(out)
    }
}

pub fn load_js_chunks(
    input_root: &Path,
    js_list_path: &Path,
) -> Result<(JsPipelineArtifact, LoadedJsChunksManifest)> {
    let js_files = parse_js_list(
        &fs::read_to_string(js_list_path)
            .with_context(|| format!("reading {}", js_list_path.display()))?,
    )?;
    let mut artifact = JsPipelineArtifact::default();
    for source_path in &js_files {
        let absolute_path = input_root.join(source_path);
        let entry_file = Path::new(source_path)
            .file_name()
            .and_then(|value| value.to_str())
            .context("source path missing file name")?
            .to_string();
        let chunk_id = chunk_id_for_js_path(source_path)?;
        let content = fs::read_to_string(&absolute_path)
            .with_context(|| format!("reading {}", absolute_path.display()))?;
        let mut files = BTreeMap::new();
        files.insert(
            entry_file.clone(),
            JsFile {
                path: entry_file.clone(),
                content: Some(content),
                ast: None,
                header_lines: Vec::new(),
                metadata: FileMetadata {
                    chunk_id: Some(chunk_id.clone()),
                    chunk_file: Some(entry_file.clone()),
                    role: Some("entry".to_string()),
                    source_path: Some(source_path.clone()),
                    ..Default::default()
                },
            },
        );
        artifact.chunk_order.push(chunk_id.clone());
        artifact.chunks.insert(
            chunk_id.clone(),
            JsChunk {
                entry_file,
                files,
                metadata: ChunkMetadata {
                    source_path: Some(source_path.clone()),
                    module_extraction_state: None,
                },
            },
        );
    }
    let manifest = LoadedJsChunksManifest {
        kind: "js.loaded_js_chunks",
        counts: LoadedCounts {
            chunks: js_files.len(),
            files: js_files.len(),
        },
        chunks: js_files
            .iter()
            .map(|source_path| {
                Ok(LoadedChunkRecord {
                    chunk_id: chunk_id_for_js_path(source_path)?,
                    entry_file: Path::new(source_path)
                        .file_name()
                        .and_then(|value| value.to_str())
                        .context("source path missing file name")?
                        .to_string(),
                    source_path: source_path.clone(),
                })
            })
            .collect::<Result<Vec<_>>>()?,
        js_files,
    };
    Ok((artifact, manifest))
}

pub fn compute_js_asts(
    artifact: &mut JsPipelineArtifact,
    drop_content: bool,
) -> Result<ComputeJsAstsManifest> {
    let keys = artifact.list_js_file_keys();
    let mut parsed = 0usize;
    for (chunk_id, file_path) in &keys {
        let chunk = artifact
            .chunks
            .get_mut(chunk_id)
            .with_context(|| format!("missing artifact chunk {chunk_id}"))?;
        let file = chunk
            .files
            .get_mut(file_path)
            .with_context(|| format!("missing artifact file {chunk_id}/{file_path}"))?;
        if file.ast.is_some() {
            continue;
        }
        let content = file
            .content
            .as_deref()
            .with_context(|| format!("computeJsAsts requires content for file: {}", file.path))?;
        file.ast = Some(parse_js_module(
            &format!("{chunk_id}/{file_path}"),
            content,
        )?);
        if drop_content {
            file.content = None;
        }
        parsed += 1;
    }
    Ok(ComputeJsAstsManifest {
        kind: "js.compute_js_asts_manifest",
        counts: ComputeJsAstsCounts {
            parsed,
            files: keys.len(),
        },
    })
}

pub fn materialize_artifact_scripts(artifact: &JsPipelineArtifact, out_dir: &Path) -> Result<()> {
    for chunk_id in artifact.list_chunk_ids() {
        let chunk = artifact
            .chunks
            .get(&chunk_id)
            .with_context(|| format!("missing artifact chunk {chunk_id}"))?;
        let chunk_out_dir = out_dir.join(split_posix_path(&chunk_id));
        fs::create_dir_all(&chunk_out_dir)?;
        for file in list_chunk_file_paths(chunk) {
            let file_artifact = chunk
                .files
                .get(&file)
                .with_context(|| format!("missing artifact file {chunk_id}/{file}"))?;
            let ast = file_artifact
                .ast
                .as_ref()
                .with_context(|| format!("artifact file has no AST: {chunk_id}/{file}"))?;
            let target_path = chunk_out_dir.join(split_posix_path(&file));
            if let Some(parent) = target_path.parent() {
                fs::create_dir_all(parent)?;
            }
            fs::write(
                &target_path,
                emit_js_module(ast, &file_artifact.header_lines)?,
            )?;
        }
        if let Some(manifest) = artifact.chunk_manifests.get(&chunk_id) {
            fs::write(
                chunk_out_dir.join("manifest.json"),
                serde_json::to_string_pretty(manifest)? + "\n",
            )?;
        }
    }
    Ok(())
}

pub fn get_chunk_entry_path(artifact: &JsPipelineArtifact, chunk_id: &str) -> Option<String> {
    let chunk = artifact.chunks.get(chunk_id)?;
    if !chunk.entry_file.is_empty() && chunk.files.contains_key(&chunk.entry_file) {
        return Some(chunk.entry_file.clone());
    }
    artifact
        .chunk_manifests
        .get(chunk_id)
        .and_then(|manifest| {
            chunk
                .files
                .contains_key(&manifest.entry_file)
                .then(|| manifest.entry_file.clone())
        })
        .or_else(|| {
            chunk.files.values().find_map(|file| {
                matches!(file.metadata.role.as_deref(), Some("entry" | "runtime"))
                    .then(|| file.path.clone())
            })
        })
        .or_else(|| chunk.files.keys().next().cloned())
}

pub fn resolve_artifact_import_reference(
    artifact: &JsPipelineArtifact,
    source: &str,
    caller_chunk_id: &str,
    caller_file: &str,
) -> Option<(String, String)> {
    if source.is_empty() || !source.starts_with('.') {
        return None;
    }
    let caller_dir = posix_join(&[caller_chunk_id, posix_dirname(caller_file).as_str()]);
    let resolved_path =
        normalize_relative_path(&posix_join(&[caller_dir.as_str(), source])).ok()?;
    for chunk_id in artifact.list_chunk_ids() {
        let Some(chunk) = artifact.chunks.get(&chunk_id) else {
            continue;
        };
        for file_path in chunk.files.keys() {
            if posix_join(&[chunk_id.as_str(), file_path.as_str()]) == resolved_path {
                return Some((chunk_id, file_path.clone()));
            }
        }
    }
    None
}

pub fn resolve_artifact_source_import_reference(
    artifact: &JsPipelineArtifact,
    source: &str,
    caller_chunk_id: &str,
    caller_file: &str,
) -> Result<Option<(String, String, String)>> {
    if source.is_empty() || (!source.starts_with('.') && !source.starts_with('/')) {
        return Ok(None);
    }
    let Some(caller_source_path) =
        source_path_for_artifact_file(artifact, caller_chunk_id, caller_file)?
    else {
        return Ok(None);
    };
    let Some(imported_source_path) =
        resolve_chunk_source_path_reference(source, &caller_source_path)
    else {
        return Ok(None);
    };
    let source_index = artifact.source_chunk_index()?;
    let Some(target_chunk_id) = source_index.get(&imported_source_path).cloned() else {
        return Ok(None);
    };
    let Some(target_entry_file) = get_chunk_entry_path(artifact, &target_chunk_id) else {
        return Ok(None);
    };
    let path = posix_join(&[target_chunk_id.as_str(), target_entry_file.as_str()]);
    Ok(Some((target_chunk_id, target_entry_file, path)))
}

pub fn relative_module_specifier(from_dir: &Path, target_path: &Path) -> String {
    let from = path_to_posix(from_dir);
    let to = path_to_posix(target_path);
    let mut specifier = posix_relative(&from, &to);
    if !specifier.starts_with('.') {
        specifier = format!("./{specifier}");
    }
    specifier
}

pub fn posix_relative(from_dir: &str, to_path: &str) -> String {
    let from_parts = from_dir
        .split('/')
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>();
    let to_parts = to_path
        .split('/')
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>();
    let mut common = 0usize;
    while common < from_parts.len()
        && common < to_parts.len()
        && from_parts[common] == to_parts[common]
    {
        common += 1;
    }
    let mut parts = Vec::new();
    for _ in common..from_parts.len() {
        parts.push("..".to_string());
    }
    for part in &to_parts[common..] {
        parts.push((*part).to_string());
    }
    if parts.is_empty() {
        ".".to_string()
    } else {
        parts.join("/")
    }
}

pub fn chunk_id_for_js_path(js_path: &str) -> Result<String> {
    let normalized = normalize_asset_path(js_path)?;
    Ok(normalized
        .strip_suffix(".js")
        .context("expected normalized .js path")?
        .to_string())
}

pub fn normalize_asset_path(path: &str) -> Result<String> {
    let normalized = normalize_relative_path(&path.replace('\\', "/"))?;
    if !normalized.ends_with(".js") {
        bail!("Expected a .js path in JS list: {path}");
    }
    Ok(normalized)
}

pub fn parse_js_list(text: &str) -> Result<Vec<String>> {
    let mut out = Vec::new();
    let mut seen = std::collections::BTreeSet::new();
    for line in text.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        let normalized = normalize_asset_path(trimmed)?;
        if !seen.insert(normalized.clone()) {
            bail!("JS list contains duplicate paths");
        }
        out.push(normalized);
    }
    Ok(out)
}

pub fn normalize_relative_path(value: &str) -> Result<String> {
    if value.is_empty() {
        bail!("Expected a non-empty relative path");
    }
    let mut parts = Vec::new();
    for part in value.split('/') {
        if part.is_empty() || part == "." {
            continue;
        }
        if part == ".." {
            if parts.pop().is_none() {
                bail!("Invalid relative path: {value}");
            }
            continue;
        }
        parts.push(part);
    }
    if parts.is_empty() || value.starts_with('/') {
        bail!("Invalid relative path: {value}");
    }
    Ok(parts.join("/"))
}

pub fn split_posix_path(path: &str) -> PathBuf {
    path.split('/').collect()
}

pub fn path_to_posix(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

/// Render `target` as a path string for inclusion in a manifest serialized at
/// `manifest_path`. If `target` is under `manifest_path`'s parent, the result
/// is relative to that parent (so the manifest tree is portable). Otherwise
/// `target` is returned verbatim. The two paths must share an anchor —
/// either both absolute or both relative to the same cwd; mixing the two
/// produces a degenerate "no common prefix" result and `target` falls
/// through unchanged.
pub fn manifest_relative_path(manifest_path: &Path, target: &Path) -> String {
    let Some(manifest_dir) = manifest_path.parent() else {
        return path_to_posix(target);
    };
    if let Ok(rel) = target.strip_prefix(manifest_dir) {
        if rel.as_os_str().is_empty() {
            return ".".to_string();
        }
        return path_to_posix(rel);
    }
    path_to_posix(target)
}

pub fn posix_join(parts: &[&str]) -> String {
    let mut out = Vec::new();
    for raw in parts {
        for part in raw.split('/') {
            if part.is_empty() || part == "." {
                continue;
            }
            if part == ".." {
                out.pop();
                continue;
            }
            out.push(part);
        }
    }
    out.join("/")
}

pub fn list_chunk_file_paths(chunk: &JsChunk) -> Vec<String> {
    let mut paths = chunk.files.keys().cloned().collect::<Vec<_>>();
    paths.sort_by(|left, right| {
        if left == &chunk.entry_file {
            std::cmp::Ordering::Less
        } else if right == &chunk.entry_file {
            std::cmp::Ordering::Greater
        } else {
            left.cmp(right)
        }
    });
    paths
}

fn posix_dirname(path: &str) -> String {
    Path::new(path)
        .parent()
        .and_then(|parent| parent.to_str())
        .unwrap_or("")
        .replace('\\', "/")
}

fn source_path_for_artifact_file(
    artifact: &JsPipelineArtifact,
    chunk_id: &str,
    file: &str,
) -> Result<Option<String>> {
    let Some(chunk) = artifact.chunks.get(chunk_id) else {
        return Ok(None);
    };
    if let Some(source_path) = chunk
        .files
        .get(file)
        .and_then(|artifact_file| artifact_file.metadata.source_path.clone())
    {
        return Ok(Some(source_path));
    }
    Ok(artifact.chunk_source_path(chunk_id))
}

fn resolve_chunk_source_path_reference(source: &str, caller_source_path: &str) -> Option<String> {
    let imported_path = if source.starts_with('/') {
        normalize_relative_path(source.trim_start_matches('/')).ok()?
    } else {
        normalize_relative_path(&posix_join(&[
            posix_dirname(caller_source_path).as_str(),
            source,
        ]))
        .ok()?
    };
    imported_path.ends_with(".js").then_some(imported_path)
}
