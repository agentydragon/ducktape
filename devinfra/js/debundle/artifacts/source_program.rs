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

    pub fn into_artifact(self) -> ChunkBundle {
        self.indexed.into_artifact()
    }
}

/// Chunk ids normalized, first occurrence kept.
pub fn normalized_chunk_ids(chunk_ids: &[String]) -> Result<Vec<String>> {
    let mut seen = BTreeSet::new();
    let mut normalized_ids = Vec::new();
    for chunk_id in chunk_ids {
        let normalized = normalize_module_path(chunk_id)?;
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
