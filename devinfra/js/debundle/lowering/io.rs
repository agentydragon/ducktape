use analysis::ProgramGraphReport;
use analysis::analysis_inputs::NativeIntegrity;
use analysis::reports::author_inputs::AuthorInputLocations;
use anyhow::{Context, Result, bail, ensure};
use graph_provenance::GraphProvenance;
use output_layout::{DEPENDENCY_GRAPH_REPORT, PROGRAM_GRAPH_REPORT};
use serde::Serialize;
use std::fs;
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

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
    if filename == output_layout::EXPORT_REENTRY_FULL_REPORT {
        return artifact::reports::write_json(path, value);
    }
    if filename == DEPENDENCY_GRAPH_REPORT {
        report_io::write_json_gzip(&path, value)?;
    } else {
        let body = serde_json::to_string_pretty(value)?;
        fs::write(path, body)?;
    }
    Ok(())
}

/// A complete-evidence stream. Call `finish` before writing its summary;
/// drop alone cannot report the final buffered write's failure.
pub(super) struct JsonlReport {
    writer: BufWriter<fs::File>,
    location: JsonlLocation,
}

enum JsonlLocation {
    Selected(PathBuf),
    Pending(tempfile::TempPath),
}

impl JsonlLocation {
    fn path(&self) -> &Path {
        match self {
            Self::Selected(path) => path,
            Self::Pending(path) => path,
        }
    }
}

impl JsonlReport {
    /// Full reports stream directly. Rejection-only streams stay temporary
    /// until the same sweep supplies the final verdict and output decision.
    pub(super) fn create(path: Option<PathBuf>) -> Result<Self> {
        let (file, location) = match path {
            Some(path) => {
                if let Some(parent) = path.parent() {
                    fs::create_dir_all(parent)
                        .with_context(|| format!("creating {}", parent.display()))?;
                }
                let file = fs::File::create(&path)
                    .with_context(|| format!("creating {}", path.display()))?;
                (file, JsonlLocation::Selected(path))
            }
            None => {
                let (file, path) = tempfile::NamedTempFile::new()?.into_parts();
                (file, JsonlLocation::Pending(path))
            }
        };
        Ok(Self {
            writer: BufWriter::new(file),
            location,
        })
    }

    pub(super) fn write(&mut self, row: &impl Serialize) -> Result<()> {
        serde_json::to_writer(&mut self.writer, row)
            .with_context(|| format!("writing {}", self.location.path().display()))?;
        self.writer
            .write_all(b"\n")
            .with_context(|| format!("writing {}", self.location.path().display()))
    }

    pub(super) fn finish(mut self, destination: Option<PathBuf>) -> Result<()> {
        flush_jsonl(&mut self.writer, self.location.path())?;
        match (&self.location, destination) {
            (JsonlLocation::Selected(path), Some(destination)) => {
                ensure!(
                    *path == destination,
                    "evidence destination changed during assessment"
                );
            }
            (JsonlLocation::Selected(_), None) => {
                bail!("selected evidence has no final report destination");
            }
            (JsonlLocation::Pending(path), Some(destination)) => {
                if let Some(parent) = destination.parent() {
                    fs::create_dir_all(parent)?;
                }
                // The temporary directory and output tree may be on different
                // filesystems. The TempPath still owns cleanup on copy errors.
                fs::copy(path, &destination)
                    .with_context(|| format!("publishing {}", destination.display()))?;
            }
            (JsonlLocation::Pending(_), None) => {}
        }
        Ok(())
    }
}

fn flush_jsonl(writer: &mut impl Write, path: &Path) -> Result<()> {
    writer
        .flush()
        .with_context(|| format!("flushing {}", path.display()))
}

/// The run's `program_graph.json.gz`, written at most once and only when a chunk
/// writes a graph that references it (a dry run's accept path writes neither).
pub(super) struct ProgramGraphOutput<'a> {
    provenance: Option<&'a GraphProvenance>,
    native_integrity: Option<&'a NativeIntegrity>,
    author_inputs: AuthorInputLocations<'a>,
    written: Mutex<bool>,
}

impl<'a> ProgramGraphOutput<'a> {
    pub(super) fn new(
        provenance: Option<&'a GraphProvenance>,
        native_integrity: Option<&'a NativeIntegrity>,
        author_inputs: AuthorInputLocations<'a>,
    ) -> Self {
        Self {
            provenance,
            native_integrity,
            author_inputs,
            written: Mutex::new(false),
        }
    }

    /// Renders author input locations into this run's reports.
    pub(super) fn author_inputs(&self) -> &AuthorInputLocations<'a> {
        &self.author_inputs
    }

    /// Ensure `program_graph.json.gz` exists under `report_out_dir` and return
    /// the reference `chunk_id`'s `dependency_graph.json.gz` records: the path
    /// relative to that graph's directory.
    pub(super) fn reference_for(&self, report_out_dir: &Path, chunk_id: &str) -> Result<PathBuf> {
        let mut written = self.written.lock().expect("program graph lock poisoned");
        if !*written {
            fs::create_dir_all(report_out_dir)?;
            let report = ProgramGraphReport {
                provenance: self.provenance.cloned(),
                native_integrity_witnesses: self
                    .native_integrity
                    .map(|integrity| {
                        let mut witnesses = integrity.witnesses.as_ref().clone();
                        for witness in &mut witnesses {
                            self.author_inputs.render_native_mutation_witness(witness);
                        }
                        witnesses
                    })
                    .unwrap_or_default(),
                native_prototype_mutations: self
                    .native_integrity
                    .map(|integrity| integrity.prototype_mutations.as_ref().clone())
                    .unwrap_or_default(),
                intrinsics_never_written_overrides: self
                    .native_integrity
                    .map(|integrity| integrity.never_written_overrides.clone())
                    .unwrap_or_default(),
            };
            report_io::write_json_gzip(&report_out_dir.join(PROGRAM_GRAPH_REPORT), &report)?;
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

#[cfg(test)]
mod jsonl_tests {
    use super::*;

    struct FlushFailure;
    impl Write for FlushFailure {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            Ok(bytes.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Err(std::io::Error::other("final write failed"))
        }
    }

    #[test]
    fn final_buffered_write_failure_is_not_reported_as_complete() {
        let error = flush_jsonl(&mut FlushFailure, Path::new("evidence.jsonl")).unwrap_err();
        assert!(error.to_string().contains("flushing evidence.jsonl"));
        assert!(error.chain().any(|cause| cause.is::<std::io::Error>()));
    }
}
