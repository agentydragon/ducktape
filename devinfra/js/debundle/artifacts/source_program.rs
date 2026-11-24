//! Prepared source files and the indexes used by resolution and analysis.

use anyhow::{Context, Result};
use js_ast::ParsedJsModule;
use source_identity::ChunkId;
use std::collections::BTreeSet;

use super::paths::normalize_module_path;

use super::indexed_files::IndexedFiles;
use super::{ArtifactIndexes, ChunkBundle, JsFile, get_chunk_entry_path};

#[derive(Clone)]
pub struct SourceProgram {
    pub(super) indexed: IndexedFiles,
}

impl SourceProgram {
    pub fn new(artifact: ChunkBundle) -> Result<Self> {
        Ok(Self {
            indexed: IndexedFiles::new(artifact)?,
        })
    }

    pub fn artifact(&self) -> &ChunkBundle {
        self.indexed.artifact()
    }

    pub fn indexes(&self) -> &ArtifactIndexes {
        self.indexed.indexes()
    }

    /// Retain read-only syntax for the selected static provider closure, while
    /// keeping emission rewriting and source-premise retention explicit.
    pub fn retain_entry_asts(
        self,
        materialized: &BTreeSet<String>,
        source_providers: &BTreeSet<String>,
        emission_asts: &BTreeSet<String>,
    ) -> Result<Self> {
        let (indexed, ()) = self.indexed.update_file_bodies(|artifact, indexes| {
            let closure = selected_static_import_closure(artifact, indexes, materialized)?;
            for chunk in &mut artifact.chunks {
                let name = artifact.chunk_table.name(chunk.chunk_id);
                if emission_asts.contains(name) {
                    continue;
                }
                let Some(pos) = chunk
                    .js
                    .files
                    .iter()
                    .position(|file| file.path == chunk.js.entry_file)
                else {
                    continue;
                };
                let file = chunk.js.files.remove(pos);
                let file =
                    if closure.contains(&chunk.chunk_id) || source_providers.contains(name) {
                        file.into_source_with_ast()
                    } else {
                        file.into_rendered_source()
                    }
                    .context("freshly prepared entry must contain an AST")?;
                chunk.js.files.insert(pos, file);
            }
            Ok(())
        })?;
        Ok(Self { indexed })
    }

    pub fn into_artifact(self) -> ChunkBundle {
        self.indexed.into_artifact()
    }
}

/// Chunk ids normalized, first occurrence kept.
pub fn normalized_chunk_ids(
    chunk_ids: impl IntoIterator<Item = impl AsRef<str>>,
) -> Result<Vec<String>> {
    let mut seen = BTreeSet::new();
    let mut normalized_ids = Vec::new();
    for chunk_id in chunk_ids {
        let normalized = normalize_module_path(chunk_id.as_ref())?;
        if seen.insert(normalized.clone()) {
            normalized_ids.push(normalized);
        }
    }
    Ok(normalized_ids)
}

/// Resolve a prepared chunk's runtime entry and its retained original AST.
pub fn chunk_runtime_entry<'a>(
    artifact: &'a ChunkBundle,
    chunk_id: &str,
    chunk_id_interned: ChunkId,
    file: Option<&str>,
) -> Result<(String, &'a JsFile, &'a ParsedJsModule)> {
    let target_file = file
        .map(normalize_module_path)
        .transpose()?
        .or_else(|| get_chunk_entry_path(artifact, chunk_id_interned))
        .with_context(|| {
            format!(
                "materialize_logical_modules could not determine entry file for chunk: {chunk_id}"
            )
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
    Ok((target_file, runtime_file, runtime_ast))
}

pub fn prune_artifact_to_chunk_ids(artifact: &mut ChunkBundle, selected: &[String]) {
    let selected_ids: std::collections::HashSet<ChunkId> = selected
        .iter()
        .filter_map(|name| artifact.chunk_table.get(name))
        .collect();
    artifact.retain_chunks(|chunk_id| selected_ids.contains(&chunk_id));
}

/// Static-request provider closure shared by frontend premise resolution and
/// staged analysis. Canonical requests include export-from dependencies, even
/// in modules with no import declarations. Including a request here grants no
/// availability proof; unsupported request modes retain their later refusals.
pub fn selected_static_import_closure(
    artifact: &ChunkBundle,
    indexes: &ArtifactIndexes,
    selected_chunk_ids: impl IntoIterator<Item = impl AsRef<str>>,
) -> Result<BTreeSet<ChunkId>> {
    let mut closure = BTreeSet::new();
    let mut pending = Vec::new();
    for chunk_id in selected_chunk_ids {
        let chunk_id = chunk_id.as_ref();
        let interned = artifact
            .chunk_table
            .get(chunk_id)
            .with_context(|| format!("materialize_logical_modules unknown chunk: {chunk_id}"))?;
        if closure.insert(interned) {
            pending.push(interned);
        }
    }

    while let Some(chunk_id) = pending.pop() {
        let Some(chunk_artifact) = artifact.find_chunk(chunk_id) else {
            continue;
        };
        let entry_file = &chunk_artifact.analysis.entry_file;
        for request in &chunk_artifact.analysis.runtime_facts.static_requests {
            let Some(target) = indexes.resolve_runtime_import_reference(
                &request.specifier,
                chunk_id,
                entry_file,
                &artifact.chunk_table,
            ) else {
                continue;
            };
            if closure.insert(target.target_chunk_id) {
                pending.push(target.target_chunk_id);
            }
        }
    }

    Ok(closure)
}
