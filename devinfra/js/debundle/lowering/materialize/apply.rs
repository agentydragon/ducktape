use std::collections::{BTreeMap, BTreeSet, HashMap};

use anyhow::{Result, bail};
use artifact::{
    ChunkAnalysisReport, ChunkArtifact, ChunkBundle, ChunkDecompositionOutput, ChunkFileRecord,
    ChunkId, ChunkLogicalModulesSummary, ChunkMetadata, JsChunk,
};

use super::MaterializedLogicalChunk;
#[cfg(test)]
use crate::{ChunkModulesCounts, ChunkModulesReport};
#[cfg(test)]
use artifact::{ChunkTable, ChunkValidationSummary};

/// Files finalized by lowering, together with decomposition metadata for
/// lowered chunks. Pass-through chunks retain their original position.
pub struct EmissionChunkOutputs {
    pub files: artifact::EmissionFiles,
    pub decomposition_by_chunk: HashMap<ChunkId, ChunkDecompositionOutput>,
}

pub(crate) fn collect_materialized_logical_chunks(
    artifact: ChunkBundle,
    target_dir: &str,
    chunks: Vec<MaterializedLogicalChunk>,
) -> Result<EmissionChunkOutputs> {
    let mut replacements = BTreeMap::<ChunkId, MaterializedLogicalChunk>::new();
    let known_chunks: BTreeSet<ChunkId> =
        artifact.chunks.iter().map(|chunk| chunk.chunk_id).collect();
    for chunk in chunks {
        let chunk_id = chunk.chunk_id;
        if !known_chunks.contains(&chunk_id) {
            bail!(
                "materialize_logical_modules produced unknown chunk index: {}",
                chunk_id.0
            );
        }
        if replacements.insert(chunk_id, chunk).is_some() {
            bail!(
                "materialize_logical_modules produced duplicate chunk_id: {}",
                artifact.chunk_table.name(chunk_id)
            );
        }
    }
    let mut decomposition_by_chunk = HashMap::new();
    let chunks = artifact
        .chunks
        .into_iter()
        .map(|chunk| {
            if let Some(replacement) = replacements.remove(&chunk.chunk_id) {
                let (output, decomposition) =
                    materialized_chunk_artifact(target_dir, chunk.analysis, replacement);
                decomposition_by_chunk.insert(chunk.chunk_id, decomposition);
                output
            } else {
                chunk
            }
        })
        .collect();
    Ok(EmissionChunkOutputs {
        files: artifact::EmissionFiles::new(ChunkBundle {
            chunks,
            chunk_table: artifact.chunk_table,
        })?,
        decomposition_by_chunk,
    })
}

pub(super) fn materialized_chunk_artifact(
    target_dir: &str,
    base_analysis: ChunkAnalysisReport,
    chunk: MaterializedLogicalChunk,
) -> (ChunkArtifact, ChunkDecompositionOutput) {
    let MaterializedLogicalChunk {
        chunk_id,
        target_file,
        source_path,
        files,
        file_records,
        applied,
        directory_dependency_facts,
        validation,
        report,
        // `unmatched_spec_claims` and `vendor_reference_rewrites` are
        // rolled up by `materialize_logical_modules` before this point;
        // downstream artifact construction doesn't carry them.
        unmatched_spec_claims: _,
        vendor_reference_rewrites: _,
    } = chunk;
    let manifest_files = file_records
        .iter()
        .map(|(file, role)| ChunkFileRecord {
            file: file.clone(),
            role: *role,
        })
        .collect();
    let logical_modules = ChunkLogicalModulesSummary {
        module_paths: report
            .final_module_contents
            .iter()
            .map(|module| module.path.clone())
            .collect(),
        target_dir: target_dir.to_string(),
    };
    let js = JsChunk {
        entry_file: target_file.clone(),
        files,
        metadata: ChunkMetadata { source_path },
    };
    let analysis = ChunkAnalysisReport {
        entry_file: target_file,
        files: manifest_files,
        ..base_analysis
    };

    let decomposition = ChunkDecompositionOutput {
        logical_modules,
        selected_module_lowerings: applied,
        directory_dependency_facts,
        validation,
    };
    (
        ChunkArtifact {
            chunk_id,
            js,
            analysis,
        },
        decomposition,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn source_chunk(chunk_id: ChunkId, name: &str) -> ChunkArtifact {
        let entry_file = "entry.js".to_string();
        let source_path = format!("{name}.js");
        ChunkArtifact {
            chunk_id,
            js: JsChunk {
                entry_file: entry_file.clone(),
                files: Vec::new(),
                metadata: ChunkMetadata {
                    source_path: source_path.clone(),
                },
            },
            analysis: ChunkAnalysisReport {
                chunk_id: name.to_string(),
                source_path,
                entry_file,
                counts: Default::default(),
                files: Vec::new(),
                imports: Vec::new(),
                export_aliases: Vec::new(),
                unresolved_exports: Vec::new(),
                kept_top_level_declarations: Vec::new(),
            },
        }
    }

    fn lowered_chunk(chunk_id: ChunkId) -> MaterializedLogicalChunk {
        MaterializedLogicalChunk {
            chunk_id,
            target_file: "lowered.js".to_string(),
            source_path: "first.js".to_string(),
            files: Vec::new(),
            file_records: Vec::new(),
            applied: Vec::new(),
            directory_dependency_facts: Vec::new(),
            validation: ChunkValidationSummary {
                linker_order: Vec::new(),
            },
            report: ChunkModulesReport {
                chunk_id: "first".to_string(),
                counts: ChunkModulesCounts {
                    applied: 0,
                    selected_owners: 0,
                },
                final_module_contents: Vec::new(),
                requested_logical_modules: Vec::new(),
                redundant_purity_hints: Vec::new(),
            },
            vendor_reference_rewrites: BTreeMap::new(),
            unmatched_spec_claims: Vec::new(),
        }
    }

    #[test]
    fn lowered_chunk_replaces_only_its_source_at_the_original_position() {
        let mut chunk_table = ChunkTable::default();
        let first = chunk_table.intern("first".to_string());
        let second = chunk_table.intern("second".to_string());
        let source = ChunkBundle {
            chunks: vec![source_chunk(second, "second"), source_chunk(first, "first")],
            chunk_table,
        };
        let output =
            collect_materialized_logical_chunks(source, "modules", vec![lowered_chunk(first)])
                .unwrap();
        assert_eq!(output.files.files().chunks[0].chunk_id, second);
        assert_eq!(
            output.files.files().chunks[0].analysis.entry_file,
            "entry.js"
        );
        assert_eq!(output.files.files().chunks[1].chunk_id, first);
        assert_eq!(
            output.files.files().chunks[1].analysis.entry_file,
            "lowered.js"
        );
        assert_eq!(
            output.files.files().chunks[1].analysis.source_path,
            "first.js"
        );
        assert_eq!(output.decomposition_by_chunk.len(), 1);
        assert!(output.decomposition_by_chunk.contains_key(&first));
    }

    #[test]
    fn unknown_lowered_chunk_is_rejected_instead_of_silently_dropped() {
        let mut chunk_table = ChunkTable::default();
        let first = chunk_table.intern("first".to_string());
        let source = ChunkBundle {
            chunks: vec![source_chunk(first, "first")],
            chunk_table,
        };
        let result =
            collect_materialized_logical_chunks(source, "", vec![lowered_chunk(ChunkId(5))]);
        assert!(result.is_err());
    }
}
