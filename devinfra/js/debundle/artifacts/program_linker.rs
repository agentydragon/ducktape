//! Builds the immutable cross-chunk binding index from syntax-derived chunk
//! facts and the artifact's existing source-path indexes.

use std::collections::{BTreeMap, BTreeSet};

use source_identity::{
    ChunkId, ProgramBindingId, ProgramChunkLinks, ProgramEvaluationKind, ProgramExportId,
    ProgramExportLink, ProgramImportKind, ProgramImportLink, ProgramLinkIndex,
    ProgramLinkResolution, ProgramModuleRequest,
};

use super::reports::{
    ChunkModuleKind, ChunkRuntimeFacts, ExportLinkKind, ExportLinkRecord, ImportLinkRecord,
    ImportSpecifierKind, StaticModuleRequest, StaticModuleRequestMode,
};
use super::{ArtifactIndexes, ChunkBundle};

/// Link facts from an already-parsed entry. Callers use this for entries that
/// the normal analysis path has already parsed while collecting other facts;
/// this builder never parses source.
#[derive(Debug, Clone, Default)]
pub struct ProgramChunkLinkFacts {
    pub module_kind: ChunkModuleKind,
    pub has_top_level_await: bool,
    pub prepared_plain_module_body: bool,
    pub requests: Vec<StaticModuleRequest>,
    pub imports: Vec<ImportLinkRecord>,
    pub exports: Vec<ExportLinkRecord>,
}

impl From<&ChunkRuntimeFacts> for ProgramChunkLinkFacts {
    fn from(facts: &ChunkRuntimeFacts) -> Self {
        Self {
            module_kind: facts.module_kind,
            has_top_level_await: facts.has_top_level_await,
            prepared_plain_module_body: facts.global_uses.is_some()
                && !facts.has_cjs_interop
                && !facts.has_own_cjs_export_surface,
            requests: facts.static_requests.clone(),
            imports: facts.import_links.clone(),
            exports: facts.export_links.clone(),
        }
    }
}

/// Normalize runtime ESM import/export syntax into a chunk-qualified program
/// index. Source specifiers are resolved only through the artifact's prepared
/// path indexes; unresolved, external, attributed, and non-entry requests stay
/// explicitly unsupported.
pub fn build_program_link_index(
    artifact: &ChunkBundle,
    indexes: &ArtifactIndexes,
    chunk_filter: Option<&BTreeSet<ChunkId>>,
    fact_overrides: &BTreeMap<ChunkId, ProgramChunkLinkFacts>,
) -> ProgramLinkIndex {
    let chunks = artifact.chunks.iter().filter_map(|chunk| {
        if chunk_filter.is_some_and(|filter| !filter.contains(&chunk.chunk_id)) {
            return None;
        }
        let facts = fact_overrides
            .get(&chunk.chunk_id)
            .cloned()
            .unwrap_or_else(|| ProgramChunkLinkFacts::from(&chunk.analysis.runtime_facts));
        let caller_file = &chunk.analysis.entry_file;
        let evaluation = if (facts.module_kind.supports_static_esm_closure()
            || facts.prepared_plain_module_body)
            && !facts.has_top_level_await
        {
            ProgramEvaluationKind::SynchronousEsm
        } else {
            ProgramEvaluationKind::Unsupported
        };
        let requests = facts
            .requests
            .iter()
            .map(|request| ProgramModuleRequest {
                source_specifier: request.specifier.clone(),
                provider: resolve_provider(
                    artifact,
                    indexes,
                    chunk.chunk_id,
                    caller_file,
                    &request.specifier,
                    request.mode == StaticModuleRequestMode::OrdinaryEsm,
                    chunk_filter,
                ),
            })
            .collect();
        let imports = facts
            .imports
            .into_iter()
            .map(|import| {
                let local = ProgramBindingId::new(chunk.chunk_id, import.local.clone());
                let provider = resolve_provider(
                    artifact,
                    indexes,
                    chunk.chunk_id,
                    caller_file,
                    &import.source,
                    import.ordinary,
                    chunk_filter,
                );
                ProgramImportLink {
                    local: local.clone(),
                    source_specifier: import.source,
                    kind: match import.kind {
                        ImportSpecifierKind::Named => ProgramImportKind::Named {
                            imported: import.imported.unwrap_or_else(|| local.local.clone()),
                        },
                        ImportSpecifierKind::Default => ProgramImportKind::Default,
                        ImportSpecifierKind::Namespace => ProgramImportKind::Namespace,
                    },
                    provider,
                }
            })
            .collect();
        let exports = facts
            .exports
            .into_iter()
            .map(|export| {
                normalize_export(
                    artifact,
                    indexes,
                    chunk.chunk_id,
                    caller_file,
                    export,
                    chunk_filter,
                )
            })
            .collect();
        Some(ProgramChunkLinks {
            chunk: chunk.chunk_id,
            evaluation,
            requests,
            imports,
            exports,
        })
    });
    ProgramLinkIndex::new(chunks)
}

fn normalize_export(
    artifact: &ChunkBundle,
    indexes: &ArtifactIndexes,
    chunk: ChunkId,
    caller_file: &str,
    record: ExportLinkRecord,
    chunk_filter: Option<&BTreeSet<ChunkId>>,
) -> ProgramExportLink {
    match record.kind {
        ExportLinkKind::Local { exported, local } => ProgramExportLink::Local {
            export: ProgramExportId::new(chunk, exported),
            binding: ProgramBindingId::new(chunk, local),
        },
        ExportLinkKind::DefaultValue => ProgramExportLink::DefaultValue {
            export: ProgramExportId::new(chunk, "default"),
            body_index: record.body_index,
        },
        ExportLinkKind::ReExport {
            exported,
            source,
            imported,
            ordinary,
        } => ProgramExportLink::ReExport {
            export: ProgramExportId::new(chunk, exported),
            provider: resolve_provider(
                artifact,
                indexes,
                chunk,
                caller_file,
                &source,
                ordinary,
                chunk_filter,
            ),
            imported,
        },
        ExportLinkKind::NamespaceReExport {
            exported,
            source,
            ordinary,
        } => ProgramExportLink::NamespaceReExport {
            export: ProgramExportId::new(chunk, exported),
            provider: resolve_provider(
                artifact,
                indexes,
                chunk,
                caller_file,
                &source,
                ordinary,
                chunk_filter,
            ),
        },
        ExportLinkKind::StarReExport { source, ordinary } => ProgramExportLink::StarReExport {
            provider: resolve_provider(
                artifact,
                indexes,
                chunk,
                caller_file,
                &source,
                ordinary,
                chunk_filter,
            ),
        },
    }
}

fn resolve_provider(
    artifact: &ChunkBundle,
    indexes: &ArtifactIndexes,
    importer: ChunkId,
    caller_file: &str,
    source: &str,
    ordinary: bool,
    chunk_filter: Option<&BTreeSet<ChunkId>>,
) -> ProgramLinkResolution<ChunkId> {
    if !ordinary {
        return ProgramLinkResolution::Unsupported;
    }
    let Some(target) = indexes.resolve_runtime_import_reference(
        source,
        importer,
        caller_file,
        &artifact.chunk_table,
    ) else {
        return if source.starts_with('.') {
            ProgramLinkResolution::Missing
        } else {
            ProgramLinkResolution::Unsupported
        };
    };
    let Some(provider) = artifact.find_chunk(target.target_chunk_id) else {
        return ProgramLinkResolution::Unsupported;
    };
    if provider.analysis.entry_file != target.target_file
        || chunk_filter.is_some_and(|filter| !filter.contains(&target.target_chunk_id))
    {
        return ProgramLinkResolution::Unsupported;
    }
    ProgramLinkResolution::Resolved(target.target_chunk_id)
}
