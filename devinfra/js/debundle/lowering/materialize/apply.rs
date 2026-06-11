use super::*;

pub(crate) struct ApplyChunksResult {
    pub(crate) artifact: ChunkBundle,
    pub(crate) decomposition_by_chunk: HashMap<ChunkId, ChunkDecompositionOutput>,
}

pub(crate) fn apply_materialized_logical_chunks(
    artifact: ChunkBundle,
    target_dir: &str,
    chunks: Vec<MaterializedLogicalChunk>,
) -> Result<ApplyChunksResult> {
    let chunk_table = artifact.chunk_table.clone();
    let mut replacements = BTreeMap::<ChunkId, MaterializedLogicalChunk>::new();
    for chunk in chunks {
        let chunk_id = chunk.chunk_id;
        if replacements.insert(chunk_id, chunk).is_some() {
            bail!(
                "materialize_logical_modules produced duplicate chunk_id: {}",
                chunk_table.name(chunk_id)
            );
        }
    }

    let source_chunks = artifact.chunks;
    let mut output_chunks = Vec::with_capacity(source_chunks.len() + replacements.len());
    let mut decomposition_by_chunk = HashMap::new();
    for chunk_artifact in source_chunks {
        if let Some(replacement) = replacements.remove(&chunk_artifact.chunk_id) {
            let (new_artifact, decomposition) = materialized_chunk_artifact(
                target_dir,
                &chunk_table,
                Some(chunk_artifact.analysis),
                replacement,
            );
            decomposition_by_chunk.insert(new_artifact.chunk_id, decomposition);
            output_chunks.push(new_artifact);
        } else {
            output_chunks.push(chunk_artifact);
        }
    }
    for replacement in replacements.into_values() {
        let (new_artifact, decomposition) =
            materialized_chunk_artifact(target_dir, &chunk_table, None, replacement);
        decomposition_by_chunk.insert(new_artifact.chunk_id, decomposition);
        output_chunks.push(new_artifact);
    }
    Ok(ApplyChunksResult {
        artifact: ChunkBundle {
            chunks: output_chunks,
            chunk_table: artifact.chunk_table,
        },
        decomposition_by_chunk,
    })
}

pub(super) fn materialized_chunk_artifact(
    target_dir: &str,
    chunk_table: &ChunkTable,
    base_analysis: Option<ChunkAnalysisReport>,
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
    let chunk_name = chunk_table.name(chunk_id).to_string();
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
        metadata: ChunkMetadata {
            source_path: source_path.clone(),
        },
    };
    let analysis = ChunkAnalysisReport {
        entry_file: target_file,
        files: manifest_files,
        ..base_analysis.unwrap_or_else(|| ChunkAnalysisReport {
            chunk_id: chunk_name,
            source_path,
            parser: Default::default(),
            entry_file: String::new(),
            counts: Default::default(),
            files: Vec::new(),
            imports: Vec::new(),
            export_aliases: Vec::new(),
            unresolved_exports: Vec::new(),
            kept_top_level_declarations: Vec::new(),
        })
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
