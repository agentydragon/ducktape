use std::collections::{BTreeSet, HashMap};
use std::path::Path;

use anyhow::{Result, bail};

use artifact::{
    ArtifactChunkRecord, ArtifactCounts, ArtifactManifest, ChunkDecompositionOutput, ChunkId,
    ChunksReport, DecompositionMetrics, EmissionFiles, PackageManifest, RootLogicalModulesSummary,
    SelectedModuleLowering, materialize_artifact_scripts, write_json,
};
use identifier_rename_queue::compute_identifier_rename_queue;
use output_layout::DebundleOutputLayout;

pub struct WriteTreeInput<'a> {
    pub files: &'a EmissionFiles,
    pub out_dir: &'a Path,
    pub lowerings: &'a [SelectedModuleLowering],
    pub counts: &'a ArtifactCounts,
    /// Chunk records of the emission set — the caller drops records of
    /// excluded chunks before passing them in.
    pub chunk_records: &'a [ArtifactChunkRecord],
    pub module_count: usize,
    pub decomposition_by_chunk: &'a HashMap<ChunkId, ChunkDecompositionOutput>,
    /// Chunks excluded from the emission set (fully vendor-swapped):
    /// their files are not written and they contribute nothing to the
    /// rename queue.
    pub excluded_chunk_ids: &'a BTreeSet<ChunkId>,
}

pub fn write_js_tree(input: &WriteTreeInput) -> Result<()> {
    if input.out_dir.as_os_str().is_empty() {
        bail!("write_js_tree requires out_dir");
    }
    let layout = DebundleOutputLayout::new(input.out_dir);
    layout.prepare()?;

    let materialized = materialize_artifact_scripts(
        input.files,
        &layout.app_root(),
        &layout.tree_root(),
        input.decomposition_by_chunk,
        input.excluded_chunk_ids,
    )?;

    let decomposition_metrics = if input.lowerings.is_empty() {
        None
    } else {
        Some(DecompositionMetrics::compute(
            input.lowerings,
            &materialized.file_metrics,
        ))
    };

    write_common_emission_reports(
        &layout,
        input.files,
        input.chunk_records,
        input.decomposition_by_chunk,
        input.excluded_chunk_ids,
    )?;
    let manifest = ArtifactManifest {
        counts: input.counts.clone(),
        chunks: input.chunk_records.to_vec(),
        logical_modules: RootLogicalModulesSummary {
            module_count: input.module_count,
        },
        selected_module_lowerings: input.lowerings.to_vec(),
        output_metrics: materialized.output_metrics,
        decomposition_metrics,
    };
    write_json(layout.output_report(), &manifest)?;

    Ok(())
}

/// The browser harness extends the same emitted tree with HTML and assets.
/// Keep its chunks report, rename queue and package marker aligned with tree
/// output.
pub fn write_common_emission_reports(
    layout: &DebundleOutputLayout,
    files: &EmissionFiles,
    chunk_records: &[ArtifactChunkRecord],
    decomposition_by_chunk: &HashMap<ChunkId, ChunkDecompositionOutput>,
    excluded_chunk_ids: &BTreeSet<ChunkId>,
) -> Result<()> {
    let queue = compute_identifier_rename_queue(files, decomposition_by_chunk, excluded_chunk_ids)?;
    write_json(layout.rename_queue_report(), &queue)?;
    write_json(
        layout.chunks_report(),
        &ChunksReport {
            chunks: chunk_records,
        },
    )?;
    write_json(
        layout.app_root().join("package.json"),
        &PackageManifest {
            module_type: "module",
        },
    )?;
    Ok(())
}
