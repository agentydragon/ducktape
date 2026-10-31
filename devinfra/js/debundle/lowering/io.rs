use analysis::ProgramGraphReport;
use analysis::analysis_inputs::NativeIntegrity;
use anyhow::{Result, bail};
use artifact::{ChunkBundle, ChunkId};
use graph_provenance::GraphProvenance;
use output_layout::{DEPENDENCY_GRAPH_REPORT, PROGRAM_GRAPH_REPORT};
use serde::Serialize;
use std::fs;
use std::io::BufWriter;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

pub(super) fn prune_artifact_to_chunk_ids(artifact: &mut ChunkBundle, selected: &[String]) {
    let selected_ids: std::collections::HashSet<ChunkId> = selected
        .iter()
        .filter_map(|name| artifact.chunk_table.get(name))
        .collect();
    artifact.retain_chunks(|chunk_id| selected_ids.contains(&chunk_id));
}

pub(super) fn chunk_report_path(report_out_dir: &Path, chunk_id: &str, filename: &str) -> PathBuf {
    report_out_dir
        .join(chunk_id.split('/').collect::<PathBuf>())
        .join(filename)
}

pub(super) fn write_chunk_report_json<T: Serialize>(
    report_out_dir: &Path,
    chunk_id: &str,
    filename: &str,
    value: &T,
) -> Result<()> {
    let path = chunk_report_path(report_out_dir, chunk_id, filename);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    if filename == DEPENDENCY_GRAPH_REPORT {
        // This side output is large enough on real app chunks that pretty
        // printing meaningfully affects local and remote test artifact size.
        // Keep small human-first reports pretty; keep the graph jq-first.
        let mut output = BufWriter::new(fs::File::create(path)?);
        serde_json::to_writer(&mut output, value)?;
    } else {
        let body = serde_json::to_string_pretty(value)?;
        fs::write(path, body)?;
    }
    Ok(())
}

/// The run's `program_graph.json`, written at most once and only when a chunk
/// writes a graph that references it (a dry run's accept path writes neither).
pub(super) struct ProgramGraphOutput<'a> {
    provenance: Option<&'a GraphProvenance>,
    native_integrity: Option<&'a NativeIntegrity>,
    written: Mutex<bool>,
}

impl<'a> ProgramGraphOutput<'a> {
    pub(super) fn new(
        provenance: Option<&'a GraphProvenance>,
        native_integrity: Option<&'a NativeIntegrity>,
    ) -> Self {
        Self {
            provenance,
            native_integrity,
            written: Mutex::new(false),
        }
    }

    /// Ensure `program_graph.json` exists under `report_out_dir` and return
    /// the reference `chunk_id`'s `dependency_graph.json` records: the path
    /// relative to that graph's directory.
    pub(super) fn reference_for(&self, report_out_dir: &Path, chunk_id: &str) -> Result<PathBuf> {
        let mut written = self.written.lock().expect("program graph lock poisoned");
        if !*written {
            fs::create_dir_all(report_out_dir)?;
            let mut output =
                BufWriter::new(fs::File::create(report_out_dir.join(PROGRAM_GRAPH_REPORT))?);
            let report = ProgramGraphReport {
                provenance: self.provenance.cloned(),
                native_integrity_witnesses: self
                    .native_integrity
                    .map(|integrity| integrity.witnesses.as_ref().clone())
                    .unwrap_or_default(),
                native_prototype_mutations: self
                    .native_integrity
                    .map(|integrity| integrity.prototype_mutations.as_ref().clone())
                    .unwrap_or_default(),
            };
            serde_json::to_writer(&mut output, &report)?;
            *written = true;
        }
        Ok(chunk_id
            .split('/')
            .map(|_| "..")
            .collect::<PathBuf>()
            .join(PROGRAM_GRAPH_REPORT))
    }
}

pub(super) fn prepare_output_dir(out_dir: &Path) -> Result<()> {
    if out_dir.exists() {
        if !out_dir.is_dir() {
            bail!(
                "Output path exists and is not a directory: {}",
                out_dir.display()
            );
        }
        if fs::read_dir(out_dir)?.next().is_some() {
            bail!(
                "Output directory is not empty: {}. Remove it before running debundle.",
                out_dir.display()
            );
        }
    }
    fs::create_dir_all(out_dir)?;
    Ok(())
}
