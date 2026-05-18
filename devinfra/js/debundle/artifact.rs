use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{Context, Result, bail};
use relative_path::RelativePath;
use serde::Serialize;

pub use analysis::{ChunkId, ChunkTable};
use js_ast::{ParsedJsModule, emit_js_module};

pub const CANONICAL_CHUNK_ENTRY_FILE: &str = "entry.js";

#[derive(Default)]
pub struct LoadedJsChunks {
    pub chunk_order: Vec<ChunkId>,
    pub chunks: Vec<Option<JsChunk>>,
    pub chunk_table: ChunkTable,
}

pub struct ChunkBundle {
    pub chunks: Vec<ChunkArtifact>,
    pub chunk_table: ChunkTable,
}

pub struct ChunkArtifact {
    pub chunk_id: ChunkId,
    pub js: JsChunk,
    pub analysis: ChunkAnalysis,
}

pub struct JsChunk {
    pub entry_file: String,
    pub files: Vec<JsFile>,
    pub metadata: ChunkMetadata,
}

impl JsChunk {
    pub fn get_file(&self, path: &str) -> Option<&JsFile> {
        self.files.iter().find(|f| f.path == path)
    }

    pub fn get_file_mut(&mut self, path: &str) -> Option<&mut JsFile> {
        self.files.iter_mut().find(|f| f.path == path)
    }

    pub fn remove_file(&mut self, path: &str) -> Option<JsFile> {
        let pos = self.files.iter().position(|f| f.path == path)?;
        Some(self.files.swap_remove(pos))
    }

    pub fn insert_file(&mut self, file: JsFile) {
        if let Some(pos) = self.files.iter().position(|f| f.path == file.path) {
            self.files[pos] = file;
        } else {
            self.files.push(file);
        }
    }

    pub fn file_paths(&self) -> impl Iterator<Item = &str> {
        self.files.iter().map(|f| f.path.as_str())
    }
}

pub struct JsFile {
    pub path: String,
    pub body: JsFileBody,
    pub header_lines: Vec<String>,
    pub metadata: FileMetadata,
}

pub struct JsFileAstParts {
    pub path: String,
    pub header_lines: Vec<String>,
    pub metadata: FileMetadata,
}

pub enum JsFileBody {
    Source(String),
    Ast(ParsedJsModule),
}

impl JsFile {
    pub fn source(&self) -> Option<&str> {
        match &self.body {
            JsFileBody::Source(source) => Some(source),
            JsFileBody::Ast(_) => None,
        }
    }

    pub fn into_source(self) -> Option<String> {
        match self.body {
            JsFileBody::Source(source) => Some(source),
            JsFileBody::Ast(_) => None,
        }
    }

    pub fn ast(&self) -> Option<&ParsedJsModule> {
        match &self.body {
            JsFileBody::Source(_) => None,
            JsFileBody::Ast(ast) => Some(ast),
        }
    }

    pub fn into_ast_parts(self) -> Option<(JsFileAstParts, ParsedJsModule)> {
        match self.body {
            JsFileBody::Ast(ast) => Some((
                JsFileAstParts {
                    path: self.path,
                    header_lines: self.header_lines,
                    metadata: self.metadata,
                },
                ast,
            )),
            JsFileBody::Source(_) => None,
        }
    }

    pub fn from_ast_parts(parts: JsFileAstParts, ast: ParsedJsModule) -> Self {
        Self {
            path: parts.path,
            body: JsFileBody::Ast(ast),
            header_lines: parts.header_lines,
            metadata: parts.metadata,
        }
    }

    /// Consume self, replacing an AST body with its rendered source text.
    /// Returns None if the body was already source text.
    pub fn into_rendered_source(self) -> Option<Self> {
        let JsFileBody::Ast(parsed) = self.body else {
            return None;
        };
        Some(Self {
            body: JsFileBody::Source(parsed.source_text()),
            path: self.path,
            header_lines: self.header_lines,
            metadata: self.metadata,
        })
    }

    pub fn is_ast(&self) -> bool {
        matches!(self.body, JsFileBody::Ast(_))
    }

    pub fn render_source(&self) -> Result<String> {
        match &self.body {
            JsFileBody::Source(source) => Ok(source.clone()),
            JsFileBody::Ast(ast) => emit_js_module(ast, &self.header_lines),
        }
    }
}

#[derive(Debug, Clone)]
pub struct ChunkMetadata {
    pub source_path: Option<String>,
}

#[derive(Debug, Clone)]
pub struct FileMetadata {
    pub chunk_id: String,
    pub chunk_file: String,
    pub role: FileRole,
    pub source_path: String,
    pub generated_by_selected_module_lowering: bool,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum FileRole {
    Entry,
    Module,
    Runtime,
}

#[derive(Debug, Clone, Serialize)]
pub struct LoadedJsChunksManifest {
    pub counts: LoadedCounts,
    pub chunks: Vec<LoadedChunkRecord>,
    pub js_files: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct LoadedCounts {
    pub chunks: usize,
    pub files: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct LoadedChunkRecord {
    pub chunk_id: String,
    pub entry_file: String,
    pub source_path: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct ParsedJsFileRecord {
    pub chunk_id: String,
    pub file: String,
    pub source_bytes: usize,
    pub parse_duration: Duration,
    pub analysis_duration: Duration,
}

#[derive(Debug, Clone, Serialize)]
pub struct ArtifactManifest {
    pub counts: ArtifactCounts,
    pub chunks: Vec<ArtifactChunkRecord>,
    pub logical_modules: RootLogicalModulesSummary,
    pub selected_module_lowerings: Vec<SelectedModuleLowering>,
    pub identifier_rename_queue: String,
    pub output_metrics: OutputMetrics,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub decomposition_metrics: Option<DecompositionMetrics>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ArtifactCounts {
    pub chunks: usize,
    pub kept_top_level_declaration_owners: usize,
    pub top_level_side_effects: usize,
    pub export_aliases: usize,
    pub unresolved_exports: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct RootLogicalModulesSummary {
    pub module_count: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChunkLogicalModulesSummary {
    pub count: usize,
    pub module_ids: Vec<String>,
    pub target_dir: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct SelectedModuleLowering {
    pub binding_names: Vec<String>,
    pub chunk_id: String,
    pub exported_names: Vec<String>,
    pub file: String,
    pub id: String,
    pub owner_ids: Vec<String>,
    pub residual: bool,
    pub target_file: String,
    pub target_path: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct ArtifactChunkRecord {
    pub chunk_id: String,
    pub source_path: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChunkAnalysis {
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
}

/// Decomposition result for a single chunk — set during `materialize_logical_modules`.
pub struct ChunkDecompositionOutput {
    pub logical_modules: ChunkLogicalModulesSummary,
    pub selected_module_lowerings: Vec<SelectedModuleLowering>,
}

/// Per-chunk manifest serialized to `<chunk>/manifest.json` at write time.
#[derive(Debug, Clone, Serialize)]
pub struct ChunkManifest {
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
    pub selected_module_lowerings: Vec<SelectedModuleLowering>,
    pub output_metrics: OutputMetrics,
}

impl ChunkManifest {
    pub fn from_analysis(
        analysis: &ChunkAnalysis,
        decomposition: Option<&ChunkDecompositionOutput>,
        output_metrics: OutputMetrics,
    ) -> Self {
        Self {
            chunk_id: analysis.chunk_id.clone(),
            source_path: analysis.source_path.clone(),
            parser: analysis.parser.clone(),
            entry_file: analysis.entry_file.clone(),
            counts: analysis.counts.clone(),
            files: analysis.files.clone(),
            imports: analysis.imports.clone(),
            export_aliases: analysis.export_aliases.clone(),
            unresolved_exports: analysis.unresolved_exports.clone(),
            kept_top_level_declarations: analysis.kept_top_level_declarations.clone(),
            logical_modules: decomposition.map(|d| d.logical_modules.clone()),
            selected_module_lowerings: decomposition
                .map(|d| d.selected_module_lowerings.clone())
                .unwrap_or_default(),
            output_metrics,
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct OutputMetrics {
    pub total: OutputSize,
    pub top_level_entry: OutputSize,
    pub named_modules: OutputSize,
    pub residual_modules: OutputSize,
    pub other_files: OutputSize,
    pub named_module_fraction: OutputFraction,
    pub residual_module_fraction: OutputFraction,
    pub top_level_entry_fraction: OutputFraction,
    pub largest_files_by_bytes: Vec<OutputFileMetric>,
}

#[derive(Debug, Clone, Serialize)]
pub struct OutputSize {
    pub files: usize,
    pub bytes: usize,
    pub lines: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct OutputFraction {
    pub bytes: f64,
    pub lines: f64,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum OutputRole {
    TopLevelEntry,
    NamedModule,
    ResidualModule,
    Other,
}

#[derive(Debug, Clone, Serialize)]
pub struct OutputFileMetric {
    pub file: String,
    pub role: OutputRole,
    pub bytes: usize,
    pub lines: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub module_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub module_path: Option<String>,
}

impl OutputMetrics {
    fn from_file_metrics(metrics: impl IntoIterator<Item = OutputFileMetric>) -> Self {
        let metrics = metrics.into_iter().collect::<Vec<_>>();
        let total = OutputSize::sum(metrics.iter());
        let top_level_entry = size_for_role(&metrics, OutputRole::TopLevelEntry);
        let named_modules = size_for_role(&metrics, OutputRole::NamedModule);
        let residual_modules = size_for_role(&metrics, OutputRole::ResidualModule);
        let other_files = size_for_role(&metrics, OutputRole::Other);
        OutputMetrics {
            named_module_fraction: output_fraction(&named_modules, &total),
            residual_module_fraction: output_fraction(&residual_modules, &total),
            top_level_entry_fraction: output_fraction(&top_level_entry, &total),
            total,
            top_level_entry,
            named_modules,
            residual_modules,
            other_files,
            largest_files_by_bytes: largest_files_by_bytes(metrics),
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct DecompositionMetrics {
    pub module_count: usize,
    pub total_symbols_defined: usize,
    pub total_exported_symbols: usize,
    pub export_ratio: f64,
    pub loc_distribution: LocDistribution,
    pub entropy: f64,
    pub per_module: Vec<ModuleDecompositionMetrics>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ModuleDecompositionMetrics {
    pub module_id: String,
    pub loc: usize,
    pub exported_symbol_count: usize,
    pub is_residual: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct LocDistribution {
    pub p50: usize,
    pub p90: usize,
    pub max: usize,
    pub min: usize,
}

impl DecompositionMetrics {
    pub fn compute(
        lowerings: &[SelectedModuleLowering],
        file_metrics: &[OutputFileMetric],
    ) -> Self {
        let mut per_module: Vec<ModuleDecompositionMetrics> = Vec::new();
        let mut total_exported = 0usize;
        let mut total_defined = 0usize;

        for lowering in lowerings {
            let loc: usize = file_metrics
                .iter()
                .filter(|f| f.module_id.as_deref() == Some(&lowering.id))
                .map(|f| f.lines)
                .sum();
            total_exported += lowering.exported_names.len();
            total_defined += lowering.binding_names.len();
            per_module.push(ModuleDecompositionMetrics {
                module_id: lowering.id.clone(),
                loc,
                exported_symbol_count: lowering.exported_names.len(),
                is_residual: lowering.residual,
            });
        }

        let module_count = per_module.len();
        let export_ratio = if total_defined > 0 {
            total_exported as f64 / total_defined as f64
        } else {
            0.0
        };

        let loc_distribution = compute_loc_distribution(&per_module);
        let entropy = compute_normalized_entropy(&per_module);

        Self {
            module_count,
            total_symbols_defined: total_defined,
            total_exported_symbols: total_exported,
            export_ratio,
            loc_distribution,
            entropy,
            per_module,
        }
    }
}

fn compute_loc_distribution(modules: &[ModuleDecompositionMetrics]) -> LocDistribution {
    let mut locs: Vec<usize> = modules.iter().map(|m| m.loc).collect();
    if locs.is_empty() {
        return LocDistribution {
            p50: 0,
            p90: 0,
            max: 0,
            min: 0,
        };
    }
    locs.sort_unstable();
    let len = locs.len();
    LocDistribution {
        p50: locs[len / 2],
        p90: locs[len * 9 / 10],
        max: locs[len - 1],
        min: locs[0],
    }
}

fn compute_normalized_entropy(modules: &[ModuleDecompositionMetrics]) -> f64 {
    if modules.len() <= 1 {
        return 0.0;
    }
    let total_loc: usize = modules.iter().map(|m| m.loc).sum();
    if total_loc == 0 {
        return 0.0;
    }
    let entropy: f64 = modules
        .iter()
        .filter(|m| m.loc > 0)
        .map(|m| {
            let p = m.loc as f64 / total_loc as f64;
            -p * p.ln()
        })
        .sum();
    let n = modules.len() as f64;
    entropy / n.ln()
}

impl OutputSize {
    fn from_file(file: &OutputFileMetric) -> Self {
        Self {
            files: 1,
            bytes: file.bytes,
            lines: file.lines,
        }
    }

    fn sum<'a>(files: impl IntoIterator<Item = &'a OutputFileMetric>) -> Self {
        files.into_iter().map(Self::from_file).fold(
            Self {
                files: 0,
                bytes: 0,
                lines: 0,
            },
            |left, right| Self {
                files: left.files + right.files,
                bytes: left.bytes + right.bytes,
                lines: left.lines + right.lines,
            },
        )
    }
}

#[derive(Debug, Clone, Serialize)]
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

#[derive(Debug, Clone, Default, Serialize)]
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
    pub role: FileRole,
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
    pub kind: ImportSpecifierKind,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub imported: Option<String>,
    pub local: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source: Option<String>,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ImportSpecifierKind {
    Named,
    Default,
    Namespace,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum ImportReferenceKind {
    ArtifactPath,
    SourcePath,
}

#[derive(Debug, Clone)]
pub struct ResolvedImportReference {
    pub kind: ImportReferenceKind,
    pub target_chunk_id: ChunkId,
    pub target_file: String,
    pub target_path: String,
}

#[derive(Debug, Clone)]
pub struct ResolvedManifestImport {
    pub caller_chunk_id: ChunkId,
    pub caller_file: String,
    pub source: String,
    pub target: ResolvedImportReference,
    pub named_imports: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ExportAliasRecord {
    pub exported: String,
    pub line: Option<usize>,
    pub local: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
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

impl LoadedJsChunks {
    pub fn list_chunk_ids(&self) -> Vec<String> {
        if self.chunk_order.is_empty() {
            self.chunks
                .iter()
                .enumerate()
                .filter_map(|(i, chunk)| {
                    chunk
                        .as_ref()
                        .map(|_| self.chunk_table.name(ChunkId(i)).to_string())
                })
                .collect()
        } else {
            self.chunk_order
                .iter()
                .map(|id| self.chunk_table.name(*id).to_string())
                .collect()
        }
    }

    pub fn take_chunk(&mut self, chunk_id: ChunkId) -> Option<JsChunk> {
        self.chunks.get_mut(chunk_id.0).and_then(|slot| slot.take())
    }
}

impl ChunkBundle {
    pub fn list_chunk_ids(&self) -> Vec<ChunkId> {
        self.chunks.iter().map(|chunk| chunk.chunk_id).collect()
    }

    pub fn list_chunk_ids_as_strings(&self) -> Vec<String> {
        self.chunks
            .iter()
            .map(|chunk| self.chunk_table.name(chunk.chunk_id).to_string())
            .collect()
    }

    pub fn has_chunk(&self, chunk_id: ChunkId) -> bool {
        self.find_chunk(chunk_id).is_some()
    }

    pub fn find_chunk(&self, chunk_id: ChunkId) -> Option<&ChunkArtifact> {
        self.chunks.iter().find(|chunk| chunk.chunk_id == chunk_id)
    }

    pub fn chunk(&self, chunk_id: ChunkId) -> Result<&ChunkArtifact> {
        self.find_chunk(chunk_id)
            .with_context(|| format!("missing artifact chunk {}", self.chunk_table.name(chunk_id)))
    }

    pub fn find_chunk_mut(&mut self, chunk_id: ChunkId) -> Option<&mut ChunkArtifact> {
        self.chunks
            .iter_mut()
            .find(|chunk| chunk.chunk_id == chunk_id)
    }

    pub fn chunk_mut(&mut self, chunk_id: ChunkId) -> Result<&mut ChunkArtifact> {
        let chunk_name = self.chunk_table.name(chunk_id).to_string();
        self.find_chunk_mut(chunk_id)
            .with_context(|| format!("missing artifact chunk {chunk_name}"))
    }

    pub fn find_js_chunk(&self, chunk_id: ChunkId) -> Option<&JsChunk> {
        self.find_chunk(chunk_id).map(|chunk| &chunk.js)
    }

    pub fn js_chunk(&self, chunk_id: ChunkId) -> Result<&JsChunk> {
        Ok(&self.chunk(chunk_id)?.js)
    }

    pub fn find_js_chunk_mut(&mut self, chunk_id: ChunkId) -> Option<&mut JsChunk> {
        self.find_chunk_mut(chunk_id).map(|chunk| &mut chunk.js)
    }

    pub fn js_chunk_mut(&mut self, chunk_id: ChunkId) -> Result<&mut JsChunk> {
        Ok(&mut self.chunk_mut(chunk_id)?.js)
    }

    pub fn remove_chunk(&mut self, chunk_id: ChunkId) -> Option<ChunkArtifact> {
        let index = self
            .chunks
            .iter()
            .position(|chunk| chunk.chunk_id == chunk_id)?;
        Some(self.chunks.remove(index))
    }

    pub fn retain_chunks(&mut self, mut keep: impl FnMut(ChunkId) -> bool) {
        self.chunks.retain(|chunk| keep(chunk.chunk_id));
    }

    pub fn chunk_source_path(&self, chunk_id: ChunkId) -> Option<String> {
        self.find_chunk(chunk_id)
            .map(|chunk| chunk.analysis.source_path.clone())
            .or_else(|| {
                self.find_js_chunk(chunk_id)
                    .and_then(|chunk| chunk.metadata.source_path.clone())
            })
            .or_else(|| Some(format!("{}.js", self.chunk_table.name(chunk_id))))
    }

    pub fn source_import_resolver<'a>(
        &'a self,
        indexes: &'a ArtifactIndexes,
    ) -> ArtifactSourceImportResolver<'a> {
        ArtifactSourceImportResolver {
            artifact: self,
            indexes,
        }
    }
}

pub struct ArtifactSourceImportResolver<'a> {
    artifact: &'a ChunkBundle,
    indexes: &'a ArtifactIndexes,
}

impl ArtifactSourceImportResolver<'_> {
    pub fn resolve(
        &self,
        source: &str,
        caller_chunk_id: ChunkId,
        caller_file: &str,
    ) -> Result<Option<(String, String, String)>> {
        if source.is_empty() || (!source.starts_with('.') && !source.starts_with('/')) {
            return Ok(None);
        }
        let Some(caller_source_path) =
            source_path_for_artifact_file(self.artifact, caller_chunk_id, caller_file)?
        else {
            return Ok(None);
        };
        let Some(imported_source_path) =
            resolve_chunk_source_path_reference(source, &caller_source_path)
        else {
            return Ok(None);
        };
        let Some(target_chunk_id) = self.indexes.chunk_id_for_source(&imported_source_path) else {
            return Ok(None);
        };
        let target_chunk_name = self.artifact.chunk_table.name(target_chunk_id);
        let Some(target_entry_file) = get_chunk_entry_path(self.artifact, target_chunk_id) else {
            return Ok(None);
        };
        let path = join_module_path(&[target_chunk_name, target_entry_file.as_str()]);
        Ok(Some((
            target_chunk_name.to_string(),
            target_entry_file,
            path,
        )))
    }
}

#[derive(Debug, Clone)]
pub struct ArtifactIndexes {
    output_path_index: HashMap<String, (ChunkId, String)>,
    source_chunk_index: HashMap<String, ChunkId>,
    chunk_source_paths: HashMap<ChunkId, String>,
    file_source_paths: HashMap<(ChunkId, String), String>,
    entry_files: HashMap<ChunkId, String>,
    file_output_paths: HashMap<(ChunkId, String), String>,
    manifest_imports_by_target_chunk: HashMap<ChunkId, Vec<ResolvedManifestImport>>,
}

impl ArtifactIndexes {
    pub fn build(artifact: &ChunkBundle) -> Result<Self> {
        let mut seen_chunk_ids = HashSet::new();
        let mut output_path_index = HashMap::new();
        let mut source_chunk_index = HashMap::new();
        let mut chunk_source_paths = HashMap::new();
        let mut file_source_paths = HashMap::new();
        let mut entry_files = HashMap::new();
        let mut file_output_paths = HashMap::new();

        for (index, chunk_artifact) in artifact.chunks.iter().enumerate() {
            let chunk_id = chunk_artifact.chunk_id;
            let chunk_name = artifact.chunk_table.name(chunk_id);
            if !seen_chunk_ids.insert(chunk_id) {
                bail!("Duplicate chunk id {chunk_name} at index {index}");
            }
            let chunk = &chunk_artifact.js;
            entry_files.insert(chunk_id, chunk.entry_file.clone());
            if let Some(source_path) = artifact.chunk_source_path(chunk_id) {
                if let Some(existing) = source_chunk_index.insert(source_path.clone(), chunk_id) {
                    bail!(
                        "Duplicate chunk sourcePath {source_path}: {} and {}",
                        artifact.chunk_table.name(existing),
                        chunk_name
                    );
                }
                chunk_source_paths.insert(chunk_id, source_path);
            }
            for file_path in list_chunk_file_paths(chunk) {
                let Some(file) = chunk.get_file(&file_path) else {
                    continue;
                };
                let key = (chunk_id, file_path.clone());
                file_source_paths.insert(key.clone(), file.metadata.source_path.clone());
                let output_path = join_module_path(&[chunk_name, &file_path]);
                if let Some(existing) = output_path_index.insert(output_path.clone(), key.clone()) {
                    bail!(
                        "Duplicate artifact output path {output_path}: {}/{} and {}/{}",
                        artifact.chunk_table.name(existing.0),
                        existing.1,
                        chunk_name,
                        key.1
                    );
                }
                file_output_paths.insert(key, output_path);
            }
        }

        let mut indexes = Self {
            output_path_index,
            source_chunk_index,
            chunk_source_paths,
            file_source_paths,
            entry_files,
            file_output_paths,
            manifest_imports_by_target_chunk: HashMap::new(),
        };
        indexes.index_manifest_imports(artifact);
        Ok(indexes)
    }

    pub fn chunk_id_for_source(&self, source_path: &str) -> Option<ChunkId> {
        self.source_chunk_index.get(source_path).copied()
    }

    fn resolve_artifact_output_reference(
        &self,
        source: &str,
        caller_chunk_name: &str,
        caller_file: &str,
    ) -> Option<(ChunkId, String)> {
        if source.is_empty() || !source.starts_with('.') {
            return None;
        }
        let caller_dir =
            join_module_path(&[caller_chunk_name, module_path_dirname(caller_file).as_str()]);
        let resolved_path =
            normalize_module_path(&join_module_path(&[caller_dir.as_str(), source])).ok()?;
        self.output_path_index.get(&resolved_path).cloned()
    }

    pub fn resolve_runtime_import_reference(
        &self,
        source: &str,
        caller_chunk_id: ChunkId,
        caller_file: &str,
        chunk_table: &ChunkTable,
    ) -> Option<ResolvedImportReference> {
        let caller_chunk_name = chunk_table.name(caller_chunk_id);
        if let Some((target_chunk_id, target_file)) =
            self.resolve_artifact_output_reference(source, caller_chunk_name, caller_file)
        {
            let target_chunk_name = chunk_table.name(target_chunk_id);
            let target_path = self
                .file_output_paths
                .get(&(target_chunk_id, target_file.clone()))
                .cloned()
                .unwrap_or_else(|| join_module_path(&[target_chunk_name, &target_file]));
            return Some(ResolvedImportReference {
                kind: ImportReferenceKind::ArtifactPath,
                target_chunk_id,
                target_file,
                target_path,
            });
        }
        self.resolve_source_path_reference(source, caller_chunk_id, caller_file, chunk_table)
    }

    fn resolve_source_path_reference(
        &self,
        source: &str,
        caller_chunk_id: ChunkId,
        caller_file: &str,
        chunk_table: &ChunkTable,
    ) -> Option<ResolvedImportReference> {
        if source.is_empty() || (!source.starts_with('.') && !source.starts_with('/')) {
            return None;
        }
        let caller_source_path = self
            .file_source_paths
            .get(&(caller_chunk_id, caller_file.to_string()))
            .or_else(|| self.chunk_source_paths.get(&caller_chunk_id))?;
        let imported_source_path = resolve_chunk_source_path_reference(source, caller_source_path)?;
        let target_chunk_id = self.source_chunk_index.get(&imported_source_path)?;
        let target_entry_file = self.entry_files.get(target_chunk_id)?.clone();
        let target_chunk_name = chunk_table.name(*target_chunk_id);
        let path = self
            .file_output_paths
            .get(&(*target_chunk_id, target_entry_file.clone()))
            .cloned()
            .unwrap_or_else(|| join_module_path(&[target_chunk_name, &target_entry_file]));
        Some(ResolvedImportReference {
            kind: ImportReferenceKind::SourcePath,
            target_chunk_id: *target_chunk_id,
            target_file: target_entry_file,
            target_path: path,
        })
    }

    pub fn manifest_imports_targeting_chunk(
        &self,
        target_chunk_id: ChunkId,
    ) -> impl Iterator<Item = &ResolvedManifestImport> {
        self.manifest_imports_by_target_chunk
            .get(&target_chunk_id)
            .into_iter()
            .flat_map(|imports| imports.iter())
    }

    fn index_manifest_imports(&mut self, artifact: &ChunkBundle) {
        for chunk in &artifact.chunks {
            let caller_chunk_id = chunk.chunk_id;
            let caller_file = chunk.analysis.entry_file.clone();
            for import in &chunk.analysis.imports {
                let Some(target) = self.resolve_runtime_import_reference(
                    &import.source,
                    caller_chunk_id,
                    &caller_file,
                    &artifact.chunk_table,
                ) else {
                    continue;
                };
                let record = ResolvedManifestImport {
                    caller_chunk_id,
                    caller_file: caller_file.clone(),
                    source: import.source.clone(),
                    target,
                    named_imports: import
                        .specifiers
                        .iter()
                        .filter(|specifier| specifier.kind == ImportSpecifierKind::Named)
                        .map(|specifier| {
                            specifier
                                .imported
                                .clone()
                                .unwrap_or_else(|| specifier.local.clone())
                        })
                        .collect(),
                };
                self.manifest_imports_by_target_chunk
                    .entry(record.target.target_chunk_id)
                    .or_default()
                    .push(record);
            }
        }
    }
}

pub fn load_js_chunks(
    input_root: &Path,
    js_list_path: &Path,
) -> Result<(LoadedJsChunks, LoadedJsChunksManifest)> {
    let js_files = parse_js_list(
        &fs::read_to_string(js_list_path)
            .with_context(|| format!("reading {}", js_list_path.display()))?,
    )?;
    let mut chunks = LoadedJsChunks::default();
    for source_path in &js_files {
        let absolute_path = input_root.join(source_path);
        let entry_file = Path::new(source_path)
            .file_name()
            .and_then(|value| value.to_str())
            .context("source path missing file name")?
            .to_string();
        let chunk_name = chunk_id_for_js_path(source_path)?;
        let chunk_id = chunks.chunk_table.intern(chunk_name.clone());
        let content = fs::read_to_string(&absolute_path)
            .with_context(|| format!("reading {}", absolute_path.display()))?;
        let files = vec![JsFile {
            path: entry_file.clone(),
            body: JsFileBody::Source(content),
            header_lines: Vec::new(),
            metadata: FileMetadata {
                chunk_id: chunk_name,
                chunk_file: entry_file.clone(),
                role: FileRole::Entry,
                source_path: source_path.clone(),
                generated_by_selected_module_lowering: false,
            },
        }];
        chunks.chunk_order.push(chunk_id);
        // Extend the vec to fit the new chunk id.
        while chunks.chunks.len() <= chunk_id.0 {
            chunks.chunks.push(None);
        }
        chunks.chunks[chunk_id.0] = Some(JsChunk {
            entry_file,
            files,
            metadata: ChunkMetadata {
                source_path: Some(source_path.clone()),
            },
        });
    }
    let manifest = LoadedJsChunksManifest {
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
    Ok((chunks, manifest))
}

pub struct MaterializedScripts {
    pub output_metrics: OutputMetrics,
    pub file_metrics: Vec<OutputFileMetric>,
}

pub fn materialize_artifact_scripts(
    artifact: &ChunkBundle,
    out_dir: &Path,
    decomposition_by_chunk: &HashMap<ChunkId, ChunkDecompositionOutput>,
) -> Result<MaterializedScripts> {
    let selected_module_by_chunk_file = selected_module_by_chunk_file(decomposition_by_chunk);
    let file_metrics: Vec<OutputFileMetric> = artifact
        .list_chunk_ids()
        .into_iter()
        .map(|chunk_id| {
            materialize_chunk_scripts(
                artifact,
                out_dir,
                &selected_module_by_chunk_file,
                decomposition_by_chunk,
                chunk_id,
            )
        })
        .collect::<Result<Vec<_>>>()?
        .into_iter()
        .flatten()
        .collect();
    Ok(MaterializedScripts {
        output_metrics: OutputMetrics::from_file_metrics(file_metrics.clone()),
        file_metrics,
    })
}

fn materialize_chunk_scripts(
    artifact: &ChunkBundle,
    out_dir: &Path,
    selected_module_by_chunk_file: &HashMap<(String, String), &SelectedModuleLowering>,
    decomposition_by_chunk: &HashMap<ChunkId, ChunkDecompositionOutput>,
    chunk_id: ChunkId,
) -> Result<Vec<OutputFileMetric>> {
    let chunk_name = artifact.chunk_table.name(chunk_id).to_string();
    let chunk_artifact = artifact.chunk(chunk_id)?;
    let chunk = &chunk_artifact.js;
    let chunk_out_dir = out_dir.join(path_from_module_path(&chunk_name));
    fs::create_dir_all(&chunk_out_dir)?;
    let metrics = list_chunk_file_paths(chunk)
        .into_iter()
        .map(|file| {
            materialize_chunk_file(
                chunk,
                &chunk_name,
                &chunk_out_dir,
                selected_module_by_chunk_file,
                file,
            )
        })
        .collect::<Result<Vec<_>>>()?;
    let metrics_output = OutputMetrics::from_file_metrics(
        metrics
            .iter()
            .map(|metric| chunk_relative_metric(&chunk_name, metric)),
    );
    let decomposition = decomposition_by_chunk.get(&chunk_id);
    let written =
        ChunkManifest::from_analysis(&chunk_artifact.analysis, decomposition, metrics_output);
    serde_json::to_writer_pretty(
        &fs::File::create(chunk_out_dir.join("manifest.json"))?,
        &written,
    )?;
    Ok(metrics)
}

fn materialize_chunk_file(
    chunk: &JsChunk,
    chunk_id: &str,
    chunk_out_dir: &Path,
    selected_module_by_chunk_file: &HashMap<(String, String), &SelectedModuleLowering>,
    file: String,
) -> Result<OutputFileMetric> {
    let file_artifact = chunk
        .get_file(&file)
        .with_context(|| format!("missing artifact file {chunk_id}/{file}"))?;
    let rendered = file_artifact.render_source()?;
    let output_path = join_module_path(&[chunk_id, &file]);
    let target_path = chunk_out_dir.join(path_from_module_path(&file));
    if let Some(parent) = target_path.parent() {
        fs::create_dir_all(parent)?;
    }
    let metric = output_file_metric(
        chunk_id,
        &output_path,
        &file,
        &rendered,
        selected_module_by_chunk_file,
        file_artifact.metadata.role,
    )?;
    fs::write(&target_path, rendered)?;
    Ok(metric)
}

fn chunk_relative_metric(chunk_id: &str, metric: &OutputFileMetric) -> OutputFileMetric {
    let prefix = format!("{chunk_id}/");
    OutputFileMetric {
        file: metric
            .file
            .strip_prefix(&prefix)
            .unwrap_or(&metric.file)
            .to_string(),
        ..metric.clone()
    }
}

fn selected_module_by_chunk_file(
    decomposition_by_chunk: &HashMap<ChunkId, ChunkDecompositionOutput>,
) -> HashMap<(String, String), &SelectedModuleLowering> {
    decomposition_by_chunk
        .values()
        .flat_map(|d| d.selected_module_lowerings.iter())
        .map(|lowering| {
            (
                (lowering.chunk_id.clone(), lowering.target_file.clone()),
                lowering,
            )
        })
        .collect()
}

fn output_file_metric(
    chunk_id: &str,
    output_path: &str,
    artifact_file_path: &str,
    rendered: &str,
    selected_module_by_chunk_file: &HashMap<(String, String), &SelectedModuleLowering>,
    role: FileRole,
) -> Result<OutputFileMetric> {
    let lowering = selected_module_by_chunk_file
        .get(&(chunk_id.to_string(), artifact_file_path.to_string()))
        .copied();
    let role = if let Some(lowering) = lowering {
        if lowering.residual {
            OutputRole::ResidualModule
        } else {
            OutputRole::NamedModule
        }
    } else {
        match role {
            FileRole::Entry => OutputRole::TopLevelEntry,
            FileRole::Module => OutputRole::NamedModule,
            FileRole::Runtime => OutputRole::Other,
        }
    };
    Ok(OutputFileMetric {
        file: output_path.to_string(),
        role,
        bytes: rendered.len(),
        lines: rendered.lines().count(),
        module_id: lowering.map(|lowering| lowering.id.clone()),
        module_path: lowering.map(|lowering| lowering.target_path.clone()),
    })
}

fn output_fraction(part: &OutputSize, total: &OutputSize) -> OutputFraction {
    OutputFraction {
        bytes: fraction(part.bytes, total.bytes),
        lines: fraction(part.lines, total.lines),
    }
}

fn size_for_role(files: &[OutputFileMetric], role: OutputRole) -> OutputSize {
    OutputSize::sum(files.iter().filter(|file| file.role == role))
}

fn largest_files_by_bytes(files: Vec<OutputFileMetric>) -> Vec<OutputFileMetric> {
    let mut sorted = files;
    sorted.sort_by(|left, right| {
        right
            .bytes
            .cmp(&left.bytes)
            .then_with(|| left.file.cmp(&right.file))
    });
    sorted.into_iter().take(20).collect()
}

fn fraction(part: usize, total: usize) -> f64 {
    if total == 0 {
        0.0
    } else {
        part as f64 / total as f64
    }
}

pub fn get_chunk_entry_path(artifact: &ChunkBundle, chunk_id: ChunkId) -> Option<String> {
    let chunk_artifact = artifact.find_chunk(chunk_id)?;
    let chunk = &chunk_artifact.js;
    if !chunk.entry_file.is_empty() && chunk.get_file(&chunk.entry_file).is_some() {
        return Some(chunk.entry_file.clone());
    }
    Some(&chunk_artifact.analysis)
        .and_then(|manifest| {
            chunk
                .get_file(&manifest.entry_file)
                .is_some()
                .then(|| manifest.entry_file.clone())
        })
        .or_else(|| {
            chunk.files.iter().find_map(|file| {
                matches!(file.metadata.role, FileRole::Entry | FileRole::Runtime)
                    .then(|| file.path.clone())
            })
        })
        .or_else(|| chunk.files.first().map(|f| f.path.clone()))
}

pub fn relative_module_specifier(from_dir: &Path, target_path: &Path) -> String {
    let from = module_path_from_path(from_dir);
    let to = module_path_from_path(target_path);
    let mut specifier = relative_module_path(&from, &to);
    if !specifier.starts_with('.') {
        specifier = format!("./{specifier}");
    }
    specifier
}

pub fn relative_module_path(from_dir: &str, to_path: &str) -> String {
    let relative = RelativePath::new(from_dir)
        .relative(RelativePath::new(to_path))
        .to_string();
    if relative.is_empty() {
        ".".to_string()
    } else {
        relative
    }
}

/// Normalize a module specifier that may have been built by string
/// concatenation (e.g. `"../" + "./foo.js"` → `".././foo.js"`). Collapses
/// `./` segments and resolves interior `..` traversals, returning a
/// canonical spelling like `"../foo.js"`. Unlike [`normalize_module_path`]
/// this preserves leading `..` components — they are legal in a relative
/// import specifier.
pub fn normalize_relative_module_specifier(value: &str) -> String {
    let normalized = RelativePath::new(value).normalize().to_string();
    if normalized.is_empty() {
        ".".to_string()
    } else {
        normalized
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
    let normalized = normalize_module_path(&path.replace('\\', "/"))?;
    if !normalized.ends_with(".js") {
        bail!("Expected a .js path in JS list: {path}");
    }
    Ok(normalized)
}

pub fn parse_js_list(text: &str) -> Result<Vec<String>> {
    let mut out = Vec::new();
    let mut seen = HashSet::new();
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

pub fn normalize_module_path(value: &str) -> Result<String> {
    if value.is_empty() || value.starts_with('/') {
        bail!("Expected a non-empty relative path");
    }
    let normalized = RelativePath::new(value).normalize();
    if normalized.as_str().is_empty() || normalized.components().any(|part| part.as_str() == "..") {
        bail!("Invalid relative path: {value}");
    }
    Ok(normalized.to_string())
}

pub fn path_from_module_path(path: &str) -> PathBuf {
    RelativePath::new(path).to_path("")
}

pub fn module_path_from_path(path: &Path) -> String {
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
        return module_path_from_path(target);
    };
    if let Ok(rel) = target.strip_prefix(manifest_dir) {
        if rel.as_os_str().is_empty() {
            return ".".to_string();
        }
        return module_path_from_path(rel);
    }
    module_path_from_path(target)
}

pub fn join_module_path(parts: &[&str]) -> String {
    parts
        .iter()
        .fold(relative_path::RelativePathBuf::new(), |base, part| {
            base.join_normalized(RelativePath::new(part))
        })
        .to_string()
}

pub fn list_chunk_file_paths(chunk: &JsChunk) -> Vec<String> {
    let mut paths = chunk
        .file_paths()
        .map(|s| s.to_string())
        .collect::<Vec<_>>();
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

pub fn module_path_dirname(path: &str) -> String {
    let path = path.replace('\\', "/");
    let normalized = RelativePath::new(&path).normalize();
    normalized
        .parent()
        .map(RelativePath::as_str)
        .unwrap_or("")
        .to_string()
}

fn source_path_for_artifact_file(
    artifact: &ChunkBundle,
    chunk_id: ChunkId,
    file: &str,
) -> Result<Option<String>> {
    let Some(chunk) = artifact.find_js_chunk(chunk_id) else {
        return Ok(None);
    };
    if let Some(artifact_file) = chunk.get_file(file) {
        return Ok(Some(artifact_file.metadata.source_path.clone()));
    }
    Ok(artifact.chunk_source_path(chunk_id))
}

pub fn resolve_chunk_source_path_reference(
    source: &str,
    caller_source_path: &str,
) -> Option<String> {
    let imported_path = if source.starts_with('/') {
        normalize_module_path(source.trim_start_matches('/')).ok()?
    } else {
        normalize_module_path(&join_module_path(&[
            module_path_dirname(caller_source_path).as_str(),
            source,
        ]))
        .ok()?
    };
    imported_path.ends_with(".js").then_some(imported_path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn module_path_dirname_normalizes_backslashes() {
        assert_eq!(module_path_dirname("static\\app\\entry.js"), "static/app");
    }

    #[test]
    fn module_path_dirname_normalizes_relative_segments() {
        assert_eq!(module_path_dirname("static/./app/entry.js"), "static/app");
    }

    #[test]
    fn module_path_dirname_handles_file_at_root() {
        assert_eq!(module_path_dirname("entry.js"), "");
    }
}
