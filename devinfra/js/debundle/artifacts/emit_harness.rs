use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::Serialize;

use artifact::{
    ArtifactChunkRecord, ChunkDecompositionOutput, ChunkId, OutputMetrics,
    materialize_artifact_scripts, module_path_from_path, write_json,
};
use output_layout::DebundleOutputLayout;
use reconstruction::ValidatedPlan;
use write_tree::write_common_emission_reports;

/// `chunk_records` carries the emission set only (the caller drops
/// records of excluded chunks); the validated plan identifies chunks
/// excluded from emission (fully vendor-swapped) — their files are not
/// written, they contribute nothing to the rename queue, and an HTML
/// entry/preload referencing one is rejected the same as a chunk the
/// snapshot manifest never contained.
pub fn emit_browser_harness(
    plan: &ValidatedPlan,
    chunk_records: &[ArtifactChunkRecord],
    decomposition_by_chunk: &HashMap<ChunkId, ChunkDecompositionOutput>,
) -> Result<()> {
    let browser = plan
        .browser_harness()
        .context("validated program has no browser harness")?;
    let files = plan.files();
    let excluded_chunk_ids = plan.excluded_chunk_ids();
    let layout = DebundleOutputLayout::new(&browser.out_dir);
    let app_root = layout.app_root();
    layout.prepare()?;
    let materialized = materialize_artifact_scripts(
        files,
        &app_root,
        &layout.tree_root(),
        decomposition_by_chunk,
        excluded_chunk_ids,
    )?;
    let copied_assets =
        copy_snapshot_assets(&browser.snapshot_root, &app_root, &browser.asset_paths)?;

    fs::write(app_root.join("index.html"), &browser.index_html)?;
    fs::write(app_root.join("bootstrap.js"), &browser.bootstrap)?;
    write_common_emission_reports(&layout, plan, chunk_records, decomposition_by_chunk)?;
    let runtime = HarnessRuntimeReport {
        app_root: format!("../{}", output_layout::APP_DIR),
        copied_assets,
        entry_scripts: browser.entry_scripts.clone(),
        module_preloads: browser.module_preloads.clone(),
        generated: HarnessGeneratedManifest {
            bootstrap: module_path_from_path(&PathBuf::from("bootstrap.js")),
            index_html: module_path_from_path(&PathBuf::from("index.html")),
        },
    };
    write_json(layout.runtime_report(), &runtime)?;
    write_json(
        layout.output_report(),
        &HarnessOutputReport {
            output_metrics: materialized.output_metrics,
        },
    )?;
    write_json(
        layout.source_assets_report(),
        &SourceAssetsReport {
            source_path: module_path_from_path(&browser.asset_summary_path),
            asset_summary: &browser.asset_summary,
        },
    )?;
    write_json(
        layout.provenance_report(),
        &ProvenanceReport {
            source_html_path: module_path_from_path(&browser.source_html_path),
            source_html: &browser.source_html,
        },
    )?;
    Ok(())
}

#[derive(Serialize)]
struct HarnessRuntimeReport {
    app_root: String,
    copied_assets: Vec<String>,
    entry_scripts: Vec<String>,
    module_preloads: Vec<String>,
    generated: HarnessGeneratedManifest,
}

#[derive(Serialize)]
struct HarnessGeneratedManifest {
    bootstrap: String,
    index_html: String,
}

#[derive(Serialize)]
struct HarnessOutputReport {
    output_metrics: OutputMetrics,
}

#[derive(Serialize)]
struct SourceAssetsReport<'a> {
    source_path: String,
    asset_summary: &'a serde_json::Value,
}

#[derive(Serialize)]
struct ProvenanceReport<'a> {
    source_html_path: String,
    source_html: &'a str,
}

fn copy_snapshot_assets(
    snapshot_root: &Path,
    out_dir: &Path,
    assets: &[PathBuf],
) -> Result<Vec<String>> {
    let mut copied = Vec::with_capacity(assets.len());
    for relative in assets {
        let out_path = out_dir.join(relative);
        if let Some(parent) = out_path.parent() {
            fs::create_dir_all(parent)?;
        }
        copy_output_file(&snapshot_root.join(relative), &out_path)?;
        copied.push(module_path_from_path(relative));
    }
    copied.sort();
    Ok(copied)
}

fn copy_output_file(source: &Path, target: &Path) -> Result<()> {
    fs::copy(source, target)?;
    make_owner_writable(target)?;
    Ok(())
}

fn make_owner_writable(path: &Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;

    let metadata = fs::metadata(path)?;
    let mut permissions = metadata.permissions();
    permissions.set_mode(permissions.mode() | 0o200);
    fs::set_permissions(path, permissions)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[cfg(unix)]
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn copied_snapshot_assets_remain_rewritable() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let snapshot = temp.path().join("snapshot");
        let out = temp.path().join("out");
        fs::create_dir_all(&snapshot)?;
        let source_html = snapshot.join("index.html");
        fs::write(&source_html, "<html></html>")?;

        #[cfg(unix)]
        fs::set_permissions(&source_html, fs::Permissions::from_mode(0o555))?;

        let copied = copy_snapshot_assets(&snapshot, &out, &[PathBuf::from("index.html")])?;

        assert_eq!(copied, vec!["index.html"]);
        fs::write(out.join("index.html"), "<html>rewritten</html>")?;
        Ok(())
    }
}
