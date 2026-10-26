use std::collections::HashMap;
use std::path::Path;

use anyhow::{Result, bail};

use artifact::ChunkId;
use artifact::metrics::DecompositionMetrics;
use artifact::reports::{
    ArtifactChunkRecord, ArtifactCounts, ArtifactManifest, ChunkDecompositionOutput, ChunksReport,
    PackageManifest, RootLogicalModulesSummary, SelectedModuleLowering, write_json,
};
use artifact::script_output::materialize_artifact_scripts;
use identifier_rename_queue::compute_identifier_rename_queue;
use output_layout::DebundleOutputLayout;
use reconstruction::ValidatedPlan;

pub struct WriteTreeInput<'a> {
    pub plan: &'a ValidatedPlan,
    pub out_dir: &'a Path,
    pub lowerings: &'a [SelectedModuleLowering],
    pub counts: &'a ArtifactCounts,
    /// Chunk records of the emission set — the caller drops records of
    /// excluded chunks before passing them in.
    pub chunk_records: &'a [ArtifactChunkRecord],
    pub module_count: usize,
    pub decomposition_by_chunk: &'a HashMap<ChunkId, ChunkDecompositionOutput>,
}

pub fn write_js_tree(input: &WriteTreeInput) -> Result<()> {
    if input.out_dir.as_os_str().is_empty() {
        bail!("write_js_tree requires out_dir");
    }
    let layout = DebundleOutputLayout::new(input.out_dir);
    layout.prepare()?;

    let materialized = materialize_artifact_scripts(
        input.plan.files(),
        &layout.app_root(),
        &layout.tree_root(),
        input.decomposition_by_chunk,
        input.plan.excluded_chunk_ids(),
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
        input.plan,
        input.chunk_records,
        input.decomposition_by_chunk,
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
    plan: &ValidatedPlan,
    chunk_records: &[ArtifactChunkRecord],
    decomposition_by_chunk: &HashMap<ChunkId, ChunkDecompositionOutput>,
) -> Result<()> {
    let files = plan.files();
    let excluded_chunk_ids = plan.excluded_chunk_ids();
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
