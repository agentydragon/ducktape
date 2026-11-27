//! Prepared source files and the indexes used by resolution and analysis.

use anyhow::{Context, Result, ensure};
use js_ast::ParsedJsModule;
use source_identity::ChunkId;
use spec::{DependencyGraphOptions, UnassignedMode};
use std::collections::{BTreeMap, BTreeSet};

use super::paths::normalize_module_path;

use super::indexed_files::IndexedFiles;
use super::{ArtifactIndexes, ChunkBundle, JsFile};

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

/// A chunk's `commuting_statements` are resolved only when its dependency
/// graph is built, so an entry for a chunk this run does not materialize would
/// never be checked. Both front ends gate on the entry before consuming it.
pub fn ensure_commuting_statements_materialize(
    chunk_analysis_options: &BTreeMap<String, DependencyGraphOptions>,
    materialized: &BTreeSet<&str>,
) -> Result<()> {
    for (chunk, options) in chunk_analysis_options {
        ensure!(
            options.commuting_statements.is_empty()
                || materialized.contains(normalize_module_path(chunk)?.as_str()),
            "chunk_analysis_options.{chunk}.commuting_statements names a chunk this run does not materialize"
        );
    }
    Ok(())
}

/// The spec validator requires an `unassigned_mode` entry for every chunk this
/// run materializes, so a miss here is a validator bug rather than a
/// recoverable spec error. Both front ends look the entry up the same way.
pub fn chunk_unassigned_mode(
    unassigned_mode: &BTreeMap<String, UnassignedMode>,
    chunk_id: &str,
) -> Result<UnassignedMode> {
    unassigned_mode.get(chunk_id).cloned().with_context(|| {
        format!("materialize_logical_modules missing unassigned_mode for chunk: {chunk_id}")
    })
}

/// Resolve a prepared chunk's runtime entry and its retained original AST.
pub fn chunk_runtime_entry<'a>(
    artifact: &'a ChunkBundle,
    chunk_id: &str,
    file: Option<&str>,
) -> Result<(ChunkId, &'a JsFile, &'a ParsedJsModule)> {
    let chunk_id_interned = artifact
        .chunk_table
        .get(chunk_id)
        .with_context(|| format!("materialize_logical_modules unknown chunk: {chunk_id}"))?;
    let runtime_file = if let Some(file) = file {
        let path = normalize_module_path(file)?;
        artifact
            .js_chunk(chunk_id_interned)?
            .get_file(&path)
            .with_context(|| {
                format!("materialize_logical_modules missing entry file for chunk: {chunk_id}")
            })?
    } else {
        artifact.find_chunk(chunk_id_interned)
            .and_then(|chunk| chunk.js.get_file(chunk.entry_path()?))
            .with_context(|| format!("materialize_logical_modules could not determine entry file for chunk: {chunk_id}"))?
    };
    let runtime_ast = runtime_file.ast().with_context(|| {
        format!("materialize_logical_modules missing entry AST for chunk: {chunk_id}")
    })?;
    Ok((chunk_id_interned, runtime_file, runtime_ast))
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
