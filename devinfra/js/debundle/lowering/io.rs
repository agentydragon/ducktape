use super::*;
use output_layout::OWNER_GRAPH_REPORT;
use std::fs;
use std::io::BufWriter;

pub(super) fn prune_artifact_to_chunk_ids(artifact: &mut ChunkBundle, selected: &[String]) {
    let selected_ids: std::collections::HashSet<ChunkId> = selected
        .iter()
        .filter_map(|name| artifact.chunk_table.get(name))
        .collect();
    artifact.retain_chunks(|chunk_id| selected_ids.contains(&chunk_id));
}

pub(super) fn write_chunk_report_json<T: Serialize>(
    report_out_dir: &Path,
    chunk_id: &str,
    filename: &str,
    value: &T,
) -> Result<()> {
    let path = report_out_dir
        .join(chunk_id.split('/').collect::<PathBuf>())
        .join(filename);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    if filename == OWNER_GRAPH_REPORT {
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
