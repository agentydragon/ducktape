//! In-memory chunk, file, and source-import index structures used by the
//! reconstruction pipeline. Filesystem path rules, report models, metrics,
//! and final script writing live in their dedicated sibling modules.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs;
use std::path::Path;

use anyhow::{Context, Result, bail};
use serde::Serialize;
use swc_common::BytePos;

pub use analysis::{ChunkId, ChunkTable};
use js_ast::{ParsedJsModule, emit_js_module_with_comments};
use paths::{
    chunk_id_for_js_path, join_module_path, module_path_dirname, normalize_module_path,
    parse_js_list, resolve_chunk_source_path_reference,
};
use reports::{ChunkAnalysisReport, ImportRecord, ImportSpecifierKind};

mod emission_files;
mod indexed_files;
pub mod metrics;
pub mod paths;
pub mod reports;
pub mod script_output;
mod source_program;
pub use emission_files::EmissionFiles;
pub use source_program::SourceProgram;

pub const CANONICAL_CHUNK_ENTRY_FILE: &str = "entry.js";

pub struct LoadedJsChunks {
    pub chunks: Vec<JsChunk>,
    pub chunk_table: ChunkTable,
}

pub struct ChunkBundle {
    pub chunks: Vec<ChunkArtifact>,
    pub chunk_table: ChunkTable,
}

pub struct ChunkArtifact {
    pub chunk_id: ChunkId,
    pub js: JsChunk,
    pub analysis: ChunkAnalysisReport,
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

    fn file_paths(&self) -> impl Iterator<Item = &str> {
        self.files.iter().map(|f| f.path.as_str())
    }
}

pub struct JsFile {
    pub path: String,
    pub body: JsFileBody,
    pub header_lines: Vec<String>,
    /// Per-binding leading comments to emit above each top-level
    /// statement that declares one of these binding names. Populated
    /// by the lowerer from `spec::Member::comment`; empty for files
    /// the spec doesn't annotate. Keys that don't match any
    /// top-level statement in the body are silently dropped at emit
    /// time. See `emit_js_module_with_comments`.
    pub binding_comments: BTreeMap<String, String>,
    /// Leading comments to emit above a specific top-level item by
    /// that item's source span low byte position. Populated by the
    /// lowerer from `anonymous_statements[].comment` after resolving
    /// the anonymous selector to the matched source statement.
    pub leading_item_comments: BTreeMap<BytePos, String>,
    pub metadata: FileMetadata,
}

pub struct JsFileAstParts {
    pub path: String,
    pub header_lines: Vec<String>,
    pub binding_comments: BTreeMap<String, String>,
    pub leading_item_comments: BTreeMap<BytePos, String>,
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

    pub fn ast_mut(&mut self) -> Option<&mut ParsedJsModule> {
        match &mut self.body {
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
                    binding_comments: self.binding_comments,
                    leading_item_comments: self.leading_item_comments,
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
            binding_comments: parts.binding_comments,
            leading_item_comments: parts.leading_item_comments,
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
            binding_comments: self.binding_comments,
            leading_item_comments: self.leading_item_comments,
            metadata: self.metadata,
        })
    }

    fn render_source(&self) -> Result<String> {
        match &self.body {
            JsFileBody::Source(source) => Ok(source.clone()),
            JsFileBody::Ast(ast) => emit_js_module_with_comments(
                ast,
                &self.header_lines,
                &self.binding_comments,
                &self.leading_item_comments,
            ),
        }
    }
}

#[derive(Debug, Clone)]
pub struct ChunkMetadata {
    pub source_path: String,
}

#[derive(Debug, Clone)]
pub struct FileMetadata {
    pub chunk_id: String,
    pub chunk_file: String,
    pub role: FileRole,
    pub source_path: String,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum FileRole {
    Entry,
    Module,
    Runtime,
}

impl ChunkBundle {
    pub fn list_chunk_ids(&self) -> Vec<ChunkId> {
        self.chunks.iter().map(|chunk| chunk.chunk_id).collect()
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

    fn find_chunk_mut(&mut self, chunk_id: ChunkId) -> Option<&mut ChunkArtifact> {
        self.chunks
            .iter_mut()
            .find(|chunk| chunk.chunk_id == chunk_id)
    }

    fn chunk_mut(&mut self, chunk_id: ChunkId) -> Result<&mut ChunkArtifact> {
        let chunk_name = self.chunk_table.name(chunk_id).to_string();
        self.find_chunk_mut(chunk_id)
            .with_context(|| format!("missing artifact chunk {chunk_name}"))
    }

    pub fn js_chunk(&self, chunk_id: ChunkId) -> Result<&JsChunk> {
        Ok(&self.chunk(chunk_id)?.js)
    }

    pub fn js_chunk_mut(&mut self, chunk_id: ChunkId) -> Result<&mut JsChunk> {
        Ok(&mut self.chunk_mut(chunk_id)?.js)
    }

    pub fn retain_chunks(&mut self, mut keep: impl FnMut(ChunkId) -> bool) {
        self.chunks.retain(|chunk| keep(chunk.chunk_id));
    }

    fn chunk_source_path(&self, chunk_id: ChunkId) -> String {
        self.find_chunk(chunk_id).map_or_else(
            || format!("{}.js", self.chunk_table.name(chunk_id)),
            |chunk| chunk.analysis.source_path.clone(),
        )
    }

    pub fn source_import_resolver<'a>(
        &'a self,
        indexes: &'a ArtifactIndexes,
    ) -> ArtifactSourceImportResolver<'a> {
        ArtifactSourceImportResolver {
            chunk_table: &self.chunk_table,
            indexes,
        }
    }
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

pub struct ArtifactSourceImportResolver<'a> {
    chunk_table: &'a ChunkTable,
    indexes: &'a ArtifactIndexes,
}

impl ArtifactSourceImportResolver<'_> {
    pub fn resolve(
        &self,
        source: &str,
        caller_chunk_id: ChunkId,
        caller_file: &str,
    ) -> Option<(String, String, String)> {
        // This caller resolves source paths only; runtime imports additionally
        // prefer already-emitted output paths. Share source resolution, not that
        // distinct precedence policy.
        let resolved = self.indexes.resolve_source_path_reference(
            source,
            caller_chunk_id,
            caller_file,
            self.chunk_table,
        )?;
        Some((
            self.chunk_table.name(resolved.target_chunk_id).to_string(),
            resolved.target_file,
            resolved.target_path,
        ))
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
            if let Some(entry) = chunk_entry_path(chunk_artifact) {
                entry_files.insert(chunk_id, entry.to_string());
            }
            let source_path = artifact.chunk_source_path(chunk_id);
            if let Some(existing) = source_chunk_index.insert(source_path.clone(), chunk_id) {
                bail!(
                    "Duplicate chunk sourcePath {source_path}: {} and {}",
                    artifact.chunk_table.name(existing),
                    chunk_name
                );
            }
            chunk_source_paths.insert(chunk_id, source_path);
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
                    named_imports: named_manifest_imports(import),
                };
                self.manifest_imports_by_target_chunk
                    .entry(record.target.target_chunk_id)
                    .or_default()
                    .push(record);
            }
        }
    }
}

/// The manifest index records only named imported bindings, not source lines
/// or default/namespace import locals.
fn named_manifest_imports(import: &ImportRecord) -> Vec<String> {
    import
        .specifiers
        .iter()
        .filter(|specifier| specifier.kind == ImportSpecifierKind::Named)
        .map(|specifier| {
            specifier
                .imported
                .clone()
                .unwrap_or_else(|| specifier.local.clone())
        })
        .collect()
}

pub fn load_js_chunks(input_root: &Path, js_list_path: &Path) -> Result<LoadedJsChunks> {
    let js_files = parse_js_list(
        &fs::read_to_string(js_list_path)
            .with_context(|| format!("reading {}", js_list_path.display()))?,
    )?;
    let mut chunk_table = ChunkTable::default();
    let mut chunks = Vec::new();
    for entry in &js_files {
        let absolute_path = input_root.join(&entry.path);
        let entry_file = Path::new(&entry.path)
            .file_name()
            .and_then(|value| value.to_str())
            .context("source path missing file name")?
            .to_string();
        let chunk_name = chunk_id_for_js_path(&entry.path)?;
        chunk_table.intern(chunk_name.clone());
        let content = fs::read_to_string(&absolute_path)
            .with_context(|| format!("reading {}", absolute_path.display()))?;
        let files = vec![JsFile {
            path: entry_file.clone(),
            body: JsFileBody::Source(content),
            header_lines: Vec::new(),
            binding_comments: BTreeMap::new(),
            leading_item_comments: BTreeMap::new(),
            metadata: FileMetadata {
                chunk_id: chunk_name,
                chunk_file: entry_file.clone(),
                role: FileRole::Entry,
                source_path: entry.source_path.clone(),
            },
        }];
        chunks.push(JsChunk {
            entry_file,
            files,
            metadata: ChunkMetadata {
                source_path: entry.source_path.clone(),
            },
        });
    }
    Ok(LoadedJsChunks {
        chunks,
        chunk_table,
    })
}

pub fn get_chunk_entry_path(artifact: &ChunkBundle, chunk_id: ChunkId) -> Option<String> {
    chunk_entry_path(artifact.find_chunk(chunk_id)?).map(str::to_string)
}

fn chunk_entry_path(chunk: &ChunkArtifact) -> Option<&str> {
    select_entry_file(
        &chunk.js.entry_file,
        &chunk.analysis.entry_file,
        chunk
            .js
            .files
            .iter()
            .map(|file| (file.path.as_str(), file.metadata.role)),
    )
}

/// Prefer a present recorded entry, then the analyzed entry, then the first
/// entry/runtime file, finally the first file. Never index a dangling entry.
fn select_entry_file<'a>(
    recorded: &str,
    analyzed: &str,
    files: impl IntoIterator<Item = (&'a str, FileRole)>,
) -> Option<&'a str> {
    files
        .into_iter()
        .min_by_key(|(path, role)| {
            if !recorded.is_empty() && *path == recorded {
                0
            } else if *path == analyzed {
                1
            } else if matches!(role, FileRole::Entry | FileRole::Runtime) {
                2
            } else {
                3
            }
        })
        .map(|(path, _)| path)
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::paths::JsListEntry;

    #[test]
    fn entry_selection_uses_present_files_and_stable_fallback_order() {
        let files = [
            ("first.js", FileRole::Runtime),
            ("analysis.js", FileRole::Entry),
            ("recorded.js", FileRole::Entry),
        ];
        assert_eq!(
            select_entry_file("recorded.js", "analysis.js", files),
            Some("recorded.js")
        );
        for recorded in ["", "missing.js"] {
            assert_eq!(
                select_entry_file(recorded, "analysis.js", files),
                Some("analysis.js")
            );
            assert_eq!(
                select_entry_file(recorded, "missing.js", files),
                Some("first.js")
            );
        }
        let modules = [("module.js", FileRole::Module)];
        assert_eq!(select_entry_file("", "", modules), Some("module.js"));
        assert_eq!(
            select_entry_file("", "", [modules[0], files[0]]),
            Some("first.js")
        );
        assert_eq!(select_entry_file("missing", "missing", []), None);
    }

    #[test]
    fn body_only_update_rejects_indexed_layout_changes() {
        let empty = || ChunkBundle {
            chunks: Vec::new(),
            chunk_table: ChunkTable::default(),
        };
        let (files, ()) = EmissionFiles::new(empty())
            .unwrap()
            .rewrite_bodies(|_, _| Ok(()))
            .unwrap();
        assert!(files.files().chunks.is_empty());

        let err = EmissionFiles::new(empty())
            .unwrap()
            .rewrite_bodies(|artifact, _| {
                artifact.chunk_table.intern("unexpected".to_string());
                Ok(())
            });
        assert!(err.is_err());
    }

    #[test]
    fn module_path_dirname_cases() {
        for (path, expected) in [
            ("static\\app\\entry.js", "static/app"),
            ("static/./app/entry.js", "static/app"),
            ("entry.js", ""),
        ] {
            assert_eq!(module_path_dirname(path), expected, "{path:?}");
        }
    }

    #[test]
    fn parse_js_list_rejects_duplicates() {
        let err = parse_js_list("a.js\na.js\n").expect_err("expected duplicate rejection");
        assert!(err.to_string().contains("duplicate"));
    }

    #[test]
    fn parse_js_list_ignores_comments_and_blank_lines() {
        let parsed = parse_js_list("\n# comment\nfoo.js\nbar.js\n").expect("parse list");
        assert_eq!(
            parsed,
            vec![
                JsListEntry {
                    path: "foo.js".into(),
                    source_path: "foo.js".into(),
                },
                JsListEntry {
                    path: "bar.js".into(),
                    source_path: "bar.js".into(),
                },
            ]
        );
    }

    #[test]
    fn parse_js_list_preserves_aliased_source_path() {
        let parsed = parse_js_list("provider.js\t$bunfs/root/chunk.js\n").expect("parse alias");
        assert_eq!(
            parsed,
            vec![JsListEntry {
                path: "provider.js".into(),
                source_path: "$bunfs/root/chunk.js".into(),
            }]
        );
        let err = parse_js_list("provider.js\toriginal.js\noriginal.js\n")
            .expect_err("source path must have one canonical chunk");
        assert!(err.to_string().contains("duplicate source paths"));
    }
}
