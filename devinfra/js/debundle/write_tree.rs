use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use serde::Serialize;

use artifact::{JsPipelineArtifact, list_chunk_file_paths, split_posix_path};
use js_ast::emit_js_module;

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct WriteJsTreeManifest {
    pub kind: &'static str,
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
    let resolved_out_dir = resolve_workspace_path(out_dir)?;
    prepare_output_dir(&resolved_out_dir, force)?;

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
            let output_path = resolved_out_dir
                .join(split_posix_path(chunk_id))
                .join(split_posix_path(&file_path));
            if let Some(parent) = output_path.parent() {
                fs::create_dir_all(parent)?;
            }
            fs::write(output_path, emit_js_module(ast, &file.header_lines)?)?;
            files.push(format!("{chunk_id}/{file_path}"));
        }
    }

    if let Some(root_manifest) = &artifact.root_manifest {
        fs::write(
            resolved_out_dir.join("manifest.json"),
            serde_json::to_string_pretty(root_manifest)? + "\n",
        )?;
    }
    for chunk_id in &chunk_ids {
        if let Some(manifest) = artifact.chunk_manifests.get(chunk_id) {
            let manifest_path = resolved_out_dir
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
        resolved_out_dir.join("package.json"),
        serde_json::to_string_pretty(&serde_json::json!({ "type": "module" }))? + "\n",
    )?;

    Ok(WriteJsTreeManifest {
        kind: "js.write_js_tree_manifest",
        out_dir: relative_workspace_path(&resolved_out_dir),
        counts: WriteJsTreeCounts {
            chunks: chunk_ids.len(),
            files: files.len(),
        },
        files,
    })
}

pub fn resolve_workspace_path(path: &Path) -> Result<PathBuf> {
    if path.is_absolute() {
        return Ok(path.to_path_buf());
    }
    let workspace = std::env::var("BUILD_WORKSPACE_DIRECTORY")
        .or_else(|_| std::env::var("BUILD_WORKING_DIRECTORY"))
        .or_else(|_| std::env::var("PWD"))
        .ok()
        .map(PathBuf::from)
        .unwrap_or(std::env::current_dir()?);
    Ok(workspace.join(path))
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

fn relative_workspace_path(path: &Path) -> String {
    let workspace = std::env::var("BUILD_WORKSPACE_DIRECTORY")
        .or_else(|_| std::env::var("BUILD_WORKING_DIRECTORY"))
        .or_else(|_| std::env::var("PWD"))
        .ok()
        .map(PathBuf::from);
    if let Some(workspace) = workspace
        && let Ok(rel) = path.strip_prefix(&workspace)
    {
        if rel.as_os_str().is_empty() {
            return path.to_string_lossy().replace('\\', "/");
        }
        return rel.to_string_lossy().replace('\\', "/");
    }
    path.to_string_lossy().replace('\\', "/")
}
