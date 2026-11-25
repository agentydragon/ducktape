//! Render finalized JavaScript artifacts and write script and tree reports.

use source_identity::ChunkId;
use std::collections::{BTreeSet, HashMap};
use std::fs;
use std::path::Path;

use anyhow::Result;
use rayon::prelude::*;
use swc_common::GLOBALS;

use artifact::metrics::{OutputFileMetric, OutputMetrics, OutputRole, write_tree_reports};
use artifact::paths::{join_module_path, path_from_module_path};
use artifact::reports::{
    ChunkDecompositionOutput, ChunkManifest, ChunkValidationSummary, SelectedModuleLowering,
};
use artifact::{ChunkArtifact, EmissionFiles, FileRole, JsFile};
use output_layout::CHUNK_REPORT;
use report_io::write_json;

pub struct MaterializedScripts {
    pub output_metrics: OutputMetrics,
    pub file_metrics: Vec<OutputFileMetric>,
}

pub(super) fn materialize_artifact_scripts(
    files: &EmissionFiles,
    app_root: &Path,
    report_tree_root: &Path,
    decomposition_by_chunk: &HashMap<ChunkId, ChunkDecompositionOutput>,
    excluded_chunk_ids: &BTreeSet<ChunkId>,
) -> Result<MaterializedScripts> {
    let artifact = files.files();
    // Per-chunk materialization is pure data-parallel: each call reads
    // `artifact` and `decomposition_by_chunk`
    // immutably, writes only into its own per-chunk output directory
    // (`app_root/<chunk_name>/...`) and its own per-chunk report path
    // under `report_tree_root`. No two chunk ids share a chunk_name (the
    // chunk_table maps each id to a unique name), so the filesystem writes
    // can't collide. `write_tree_reports` below is a separate sequential
    // phase that consumes the collected `file_metrics`.
    //
    // `swc_common::GLOBALS` is a `scoped_tls` thread-local that does NOT
    // carry into rayon worker threads, and `render_source()` on
    // `JsFileBody::Ast` calls `emit_js_module_with_comments`, which drives
    // the swc emitter. Capture the parent thread's globals and re-set
    // inside each worker closure — mirrors `prepare_candidate_emission_plan` and
    // `vendor::emission::apply_emission_rewrites_in_place`.
    let file_metrics: Vec<OutputFileMetric> = GLOBALS
        .with(|globals| -> Result<Vec<_>> {
            artifact
                .chunks
                .par_iter()
                // Emission-set exclusion: fully vendor-swapped chunks
                // stay in the bundle but are not emitted.
                .filter(|chunk| !excluded_chunk_ids.contains(&chunk.chunk_id))
                .map(|chunk| {
                    GLOBALS.set(globals, || {
                        materialize_chunk_scripts(
                            chunk,
                            artifact.chunk_table.name(chunk.chunk_id),
                            app_root,
                            report_tree_root,
                            decomposition_by_chunk.get(&chunk.chunk_id),
                        )
                    })
                })
                .collect::<Result<Vec<_>>>()
        })?
        .into_iter()
        .flatten()
        .collect();
    write_tree_reports(report_tree_root, decomposition_by_chunk, &file_metrics)?;
    Ok(MaterializedScripts {
        output_metrics: OutputMetrics::from_file_metrics(file_metrics.clone()),
        file_metrics,
    })
}

fn materialize_chunk_scripts(
    chunk_artifact: &ChunkArtifact,
    chunk_name: &str,
    app_root: &Path,
    report_tree_root: &Path,
    decomposition: Option<&ChunkDecompositionOutput>,
) -> Result<Vec<OutputFileMetric>> {
    let lowerings = decomposition.map_or(&[][..], |d| d.selected_module_lowerings.as_slice());
    let by_file: HashMap<_, _> = lowerings
        .iter()
        .map(|lowering| (lowering.target_file.as_str(), lowering))
        .collect();
    let chunk = &chunk_artifact.js;
    let chunk_out_dir = app_root.join(path_from_module_path(chunk_name));
    fs::create_dir_all(&chunk_out_dir)?;
    let metrics = chunk
        .ordered_files()
        .into_iter()
        .map(|file| {
            materialize_chunk_file(
                chunk_name,
                &chunk_out_dir,
                by_file.get(file.path.as_str()).copied(),
                file,
            )
        })
        .collect::<Result<Vec<_>>>()?;
    let metrics_output = OutputMetrics::from_file_metrics(
        metrics
            .iter()
            .map(|metric| chunk_relative_metric(chunk_name, metric)),
    );
    let empty_validation = ChunkValidationSummary {
        linker_order: Vec::new(),
    };
    let written = ChunkManifest {
        analysis: &chunk_artifact.analysis,
        validation: decomposition.map_or(&empty_validation, |d| &d.validation),
        logical_modules: decomposition.map(|d| &d.logical_modules),
        selected_module_lowerings: lowerings,
        output_metrics: metrics_output,
    };
    write_json(
        report_tree_root
            .join(path_from_module_path(chunk_name))
            .join(CHUNK_REPORT),
        &written,
    )?;
    Ok(metrics)
}

fn materialize_chunk_file(
    chunk_id: &str,
    chunk_out_dir: &Path,
    lowering: Option<&SelectedModuleLowering>,
    file_artifact: &JsFile,
) -> Result<OutputFileMetric> {
    let file = file_artifact.path.as_str();
    let rendered = file_artifact.render_source()?;
    let output_path = join_module_path(&[chunk_id, file]);
    let target_path = chunk_out_dir.join(path_from_module_path(file));
    if let Some(parent) = target_path.parent() {
        fs::create_dir_all(parent)?;
    }
    let metric = output_file_metric(
        &output_path,
        &rendered,
        lowering,
        file_artifact.metadata.role,
    );
    fs::write(&target_path, rendered)?;
    Ok(metric)
}

fn chunk_relative_metric(chunk_id: &str, metric: &OutputFileMetric) -> OutputFileMetric {
    let prefix = format!("{chunk_id}/");
    OutputFileMetric {
        file: metric
            .file
            .strip_prefix(&prefix)
            .unwrap_or(&metric.file)
            .to_string(),
        ..metric.clone()
    }
}

fn output_file_metric(
    output_path: &str,
    rendered: &str,
    lowering: Option<&SelectedModuleLowering>,
    role: FileRole,
) -> OutputFileMetric {
    let role = if let Some(lowering) = lowering {
        if lowering.residual {
            OutputRole::ResidualModule
        } else {
            OutputRole::NamedModule
        }
    } else {
        match role {
            FileRole::Entry => OutputRole::TopLevelEntry,
            FileRole::Module => OutputRole::NamedModule,
            FileRole::Runtime => OutputRole::Other,
        }
    };
    OutputFileMetric {
        file: output_path.to_string(),
        role,
        bytes: rendered.len(),
        lines: rendered.lines().count(),
        module: lowering.map(SelectedModuleLowering::module_ref),
    }
}
