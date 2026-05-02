use std::fs;
use std::path::Path;

use anyhow::{Context, Result, bail};
use serde::Serialize;

use artifact::{
    JsPipelineArtifact, list_chunk_file_paths, manifest_relative_path, split_posix_path,
};
use js_ast::emit_js_module;
use scrambled_id_frequencies::{compute_scrambled_identifier_frequencies, write_queue};

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct WriteJsTreeManifest {
    pub kind: &'static str,
    /// Always `"."` — the manifest sits at `<out_dir>/manifest.json`, so
    /// `out_dir` is the manifest's own directory. Recorded explicitly so
    /// downstream readers can confirm the manifest's role.
    pub out_dir: String,
    pub counts: WriteJsTreeCounts,
    pub files: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct WriteJsTreeCounts {
    pub chunks: usize,
    pub files: usize,
}

pub fn write_js_tree(
    artifact: &JsPipelineArtifact,
    out_dir: &Path,
    force: bool,
) -> Result<WriteJsTreeManifest> {
    if out_dir.as_os_str().is_empty() {
        bail!("writeJsTree requires outDir");
    }
    prepare_output_dir(out_dir, force)?;

    let chunk_ids = artifact.list_chunk_ids();
    let mut files = Vec::new();
    for chunk_id in &chunk_ids {
        let chunk = artifact
            .chunks
            .get(chunk_id)
            .with_context(|| format!("missing artifact chunk {chunk_id}"))?;
        for file_path in list_chunk_file_paths(chunk) {
            let file = chunk
                .files
                .get(&file_path)
                .with_context(|| format!("missing artifact file {chunk_id}/{file_path}"))?;
            let ast = file
                .ast
                .as_ref()
                .with_context(|| format!("artifact file has no AST: {chunk_id}/{file_path}"))?;
            let output_path = out_dir
                .join(split_posix_path(chunk_id))
                .join(split_posix_path(&file_path));
            if let Some(parent) = output_path.parent() {
                fs::create_dir_all(parent)?;
            }
            fs::write(output_path, emit_js_module(ast, &file.header_lines)?)?;
            files.push(format!("{chunk_id}/{file_path}"));
        }
    }

    // The scrambled-identifier frequency queue is a side output of every
    // pipeline run that writes a tree manifest. Emit it now and record
    // its manifest-relative path on the root manifest.
    let queue = compute_scrambled_identifier_frequencies(artifact)?;
    let queue_path = write_queue(out_dir, &queue)?;
    if let Some(root_manifest) = &artifact.root_manifest {
        let manifest_path = out_dir.join("manifest.json");
        let mut root_manifest = root_manifest.clone();
        root_manifest.scrambled_identifier_frequencies =
            Some(manifest_relative_path(&manifest_path, &queue_path));
        fs::write(
            &manifest_path,
            serde_json::to_string_pretty(&root_manifest)? + "\n",
        )?;
    }
    for chunk_id in &chunk_ids {
        if let Some(manifest) = artifact.chunk_manifests.get(chunk_id) {
            let manifest_path = out_dir
                .join(split_posix_path(chunk_id))
                .join("manifest.json");
            if let Some(parent) = manifest_path.parent() {
                fs::create_dir_all(parent)?;
            }
            fs::write(
                manifest_path,
                serde_json::to_string_pretty(manifest)? + "\n",
            )?;
        }
    }
    fs::write(
        out_dir.join("package.json"),
        serde_json::to_string_pretty(&serde_json::json!({ "type": "module" }))? + "\n",
    )?;

    Ok(WriteJsTreeManifest {
        kind: "js.write_js_tree_manifest",
        out_dir: ".".to_string(),
        counts: WriteJsTreeCounts {
            chunks: chunk_ids.len(),
            files: files.len(),
        },
        files,
    })
}

fn prepare_output_dir(out_dir: &Path, force: bool) -> Result<()> {
    if out_dir.exists() {
        let metadata = fs::metadata(out_dir)?;
        if !metadata.is_dir() {
            bail!(
                "Output path exists and is not a directory: {}",
                out_dir.display()
            );
        }
        if fs::read_dir(out_dir)?.next().is_some() && !force {
            bail!(
                "Output directory is not empty: {}. Pass --force to replace it.",
                out_dir.display()
            );
        }
        if force {
            fs::remove_dir_all(out_dir)?;
        }
    }
    fs::create_dir_all(out_dir)?;
    Ok(())
}
