//! Render finalized JavaScript artifacts and write script and tree reports.

use std::collections::{BTreeSet, HashMap};
use std::fs;
use std::path::Path;

use anyhow::{Context, Result};
use rayon::prelude::*;
use swc_common::GLOBALS;

use crate::metrics::{OutputFileMetric, OutputMetrics, OutputRole, write_tree_reports};
use crate::paths::{join_module_path, path_from_module_path};
use crate::reports::{ChunkDecompositionOutput, ChunkManifest, SelectedModuleLowering, write_json};
use crate::{ChunkBundle, ChunkId, EmissionFiles, FileRole, JsChunk, list_chunk_file_paths};
use output_layout::CHUNK_REPORT;

pub struct MaterializedScripts {
    pub output_metrics: OutputMetrics,
    pub file_metrics: Vec<OutputFileMetric>,
}

pub fn materialize_artifact_scripts(
    files: &EmissionFiles,
    app_root: &Path,
    report_tree_root: &Path,
    decomposition_by_chunk: &HashMap<ChunkId, ChunkDecompositionOutput>,
    excluded_chunk_ids: &BTreeSet<ChunkId>,
) -> Result<MaterializedScripts> {
    let artifact = files.files();
    let selected_module_by_chunk_file = selected_module_by_chunk_file(decomposition_by_chunk);
    // Per-chunk materialization is pure data-parallel: each call reads
    // `artifact`/`decomposition_by_chunk`/`selected_module_by_chunk_file`
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
    // inside each worker closure — mirrors `lower_chunk` and
    // `vendor::emission::apply_emission_rewrites`.
    let file_metrics: Vec<OutputFileMetric> = GLOBALS
        .with(|globals| -> Result<Vec<_>> {
            artifact
                .list_chunk_ids()
                .into_par_iter()
                // Emission-set exclusion: fully vendor-swapped chunks
                // stay in the bundle but are not emitted.
                .filter(|chunk_id| !excluded_chunk_ids.contains(chunk_id))
                .map(|chunk_id| {
                    GLOBALS.set(globals, || {
                        materialize_chunk_scripts(
                            artifact,
                            app_root,
                            report_tree_root,
                            &selected_module_by_chunk_file,
                            decomposition_by_chunk,
                            chunk_id,
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
    artifact: &ChunkBundle,
    app_root: &Path,
    report_tree_root: &Path,
    selected_module_by_chunk_file: &HashMap<(String, String), &SelectedModuleLowering>,
    decomposition_by_chunk: &HashMap<ChunkId, ChunkDecompositionOutput>,
    chunk_id: ChunkId,
) -> Result<Vec<OutputFileMetric>> {
    let chunk_name = artifact.chunk_table.name(chunk_id).to_string();
    let chunk_artifact = artifact.chunk(chunk_id)?;
    let chunk = &chunk_artifact.js;
    let chunk_out_dir = app_root.join(path_from_module_path(&chunk_name));
    fs::create_dir_all(&chunk_out_dir)?;
    let metrics = list_chunk_file_paths(chunk)
        .into_iter()
        .map(|file| {
            materialize_chunk_file(
                chunk,
                &chunk_name,
                &chunk_out_dir,
                selected_module_by_chunk_file,
                file,
            )
        })
        .collect::<Result<Vec<_>>>()?;
    let metrics_output = OutputMetrics::from_file_metrics(
        metrics
            .iter()
            .map(|metric| chunk_relative_metric(&chunk_name, metric)),
    );
    let decomposition = decomposition_by_chunk.get(&chunk_id);
    let written =
        ChunkManifest::from_analysis(&chunk_artifact.analysis, decomposition, metrics_output);
    write_json(
        report_tree_root
            .join(path_from_module_path(&chunk_name))
            .join(CHUNK_REPORT),
        &written,
    )?;
    Ok(metrics)
}

fn materialize_chunk_file(
    chunk: &JsChunk,
    chunk_id: &str,
    chunk_out_dir: &Path,
    selected_module_by_chunk_file: &HashMap<(String, String), &SelectedModuleLowering>,
    file: String,
) -> Result<OutputFileMetric> {
    let file_artifact = chunk
        .get_file(&file)
        .with_context(|| format!("missing artifact file {chunk_id}/{file}"))?;
    let rendered = file_artifact.render_source()?;
    let output_path = join_module_path(&[chunk_id, &file]);
    let target_path = chunk_out_dir.join(path_from_module_path(&file));
    if let Some(parent) = target_path.parent() {
        fs::create_dir_all(parent)?;
    }
    let metric = output_file_metric(
        chunk_id,
        &output_path,
        &file,
        &rendered,
        selected_module_by_chunk_file,
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

fn selected_module_by_chunk_file(
    decomposition_by_chunk: &HashMap<ChunkId, ChunkDecompositionOutput>,
) -> HashMap<(String, String), &SelectedModuleLowering> {
    decomposition_by_chunk
        .values()
        .flat_map(|d| d.selected_module_lowerings.iter())
        .map(|lowering| {
            (
                (lowering.chunk_id.clone(), lowering.target_file.clone()),
                lowering,
            )
        })
        .collect()
}

fn output_file_metric(
    chunk_id: &str,
    output_path: &str,
    artifact_file_path: &str,
    rendered: &str,
    selected_module_by_chunk_file: &HashMap<(String, String), &SelectedModuleLowering>,
    role: FileRole,
) -> OutputFileMetric {
    let lowering = selected_module_by_chunk_file
        .get(&(chunk_id.to_string(), artifact_file_path.to_string()))
        .copied();
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
