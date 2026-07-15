use std::collections::{BTreeSet, HashMap};
use std::fs;
use std::path::Path;

use anyhow::{Result, bail};

use artifact::{
    ArtifactChunkRecord, ArtifactCounts, ArtifactManifest, ChunkBundle, ChunkDecompositionOutput,
    ChunkId, ChunksReport, DecompositionMetrics, PackageManifest, RootLogicalModulesSummary,
    SelectedModuleLowering, materialize_artifact_scripts, write_json,
};
use identifier_rename_queue::{compute_identifier_rename_queue, write_queue};
use output_layout::DebundleOutputLayout;

pub struct WriteTreeInput<'a> {
    pub artifact: &'a ChunkBundle,
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
    prepare_output_layout(&layout)?;

    let materialized = materialize_artifact_scripts(
        input.artifact,
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

    let queue = compute_identifier_rename_queue(
        input.artifact,
        input.decomposition_by_chunk,
        input.excluded_chunk_ids,
    )?;
    write_queue(&layout.rename_queue_report(), &queue)?;
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
    write_json(
        layout.chunks_report(),
        &ChunksReport {
            chunks: input.chunk_records,
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

fn prepare_output_layout(layout: &DebundleOutputLayout) -> Result<()> {
    if layout.root().exists() {
        let metadata = fs::metadata(layout.root())?;
        if !metadata.is_dir() {
            bail!(
                "Output path exists and is not a directory: {}",
                layout.root().display()
            );
        }
    }
    let app_root = layout.app_root();
    fs::create_dir_all(app_root)?;
    fs::create_dir_all(layout.reports_root())?;
    Ok(())
}
