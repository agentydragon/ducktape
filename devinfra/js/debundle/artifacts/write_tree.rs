use std::collections::{BTreeSet, HashMap};
use std::fs;
use std::path::Path;

use anyhow::{Result, bail};

use artifact::metrics::DecompositionMetrics;
use artifact::reports::{
    ArtifactChunkRecord, ArtifactCounts, ArtifactManifest, ChunkDecompositionOutput, ChunksReport,
    PackageManifest, RootLogicalModulesSummary, SelectedModuleLowering, write_json,
};
use artifact::script_output::materialize_artifact_scripts;
use artifact::{ChunkId, EmissionFiles};
use identifier_rename_queue::compute_identifier_rename_queue;
use output_layout::{DebundleOutputLayout, GATE_REJECTED_MARKER};
use reconstruction::{EmittablePlan, RejectedPlan};

pub struct WriteTreeInput<'a> {
    pub plan: EmittablePlan<'a>,
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
    // The marker precedes the files it describes, so an interrupted write
    // never leaves an unmarked partial tree behind.
    if let EmittablePlan::Rejected(rejected) = input.plan {
        fs::write(
            layout.gate_rejected_marker(),
            render_gate_rejected_marker(rejected),
        )?;
    }

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
        input.plan.files(),
        input.plan.excluded_chunk_ids(),
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

/// The `GATE_REJECTED` marker: why the tree beside it is not runnable, and
/// where the gate's evidence is.
fn render_gate_rejected_marker(rejected: &RejectedPlan) -> String {
    let mut text = format!(
        "{GATE_REJECTED_MARKER}: this tree is NOT runnable.\n\
         The realizability gate rejected the spec; `debundle run --emit-on-rejection` wrote the \
         logical modules for reading only. No browser harness or vendor replacement assets were \
         written. Fix the spec and rerun without the flag for runnable output.\n\n"
    );
    for rejection in rejected.rejections() {
        text.push_str(&rejection.summary_line());
        text.push('\n');
        if let Some(path) = &rejection.dependency_graph_report {
            text.push_str(&format!("  dependency graph: {}\n", path.display()));
        }
    }
    text.push('\n');
    text.push_str(&rejected.diagnostic());
    text.push('\n');
    text
}

/// The browser harness extends the same emitted tree with HTML and assets.
/// Keep its chunks report, rename queue and package marker aligned with tree
/// output.
pub fn write_common_emission_reports(
    layout: &DebundleOutputLayout,
    files: &EmissionFiles,
    excluded_chunk_ids: &BTreeSet<ChunkId>,
    chunk_records: &[ArtifactChunkRecord],
    decomposition_by_chunk: &HashMap<ChunkId, ChunkDecompositionOutput>,
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
