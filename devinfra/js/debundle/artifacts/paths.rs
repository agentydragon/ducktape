//! Canonical module-path and JS-list parsing helpers shared by artifact
//! loading, import resolution, report generation, and script output.

use std::collections::HashSet;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use relative_path::RelativePath;

pub fn relative_module_specifier(from_dir: &Path, target_path: &Path) -> String {
    let from = module_path_from_path(from_dir);
    let to = module_path_from_path(target_path);
    let mut specifier = relative_module_path(&from, &to);
    if !specifier.starts_with('.') {
        specifier = format!("./{specifier}");
    }
    specifier
}

pub fn relative_module_path(from_dir: &str, to_path: &str) -> String {
    let relative = RelativePath::new(from_dir)
        .relative(RelativePath::new(to_path))
        .to_string();
    if relative.is_empty() {
        ".".to_string()
    } else {
        relative
    }
}

/// Normalize a module specifier that may have been built by string
/// concatenation (e.g. `"../" + "./foo.js"` → `".././foo.js"`). Collapses
/// `./` segments and resolves interior `..` traversals, returning a
/// canonical spelling like `"../foo.js"`. Unlike [`normalize_module_path`]
/// this preserves leading `..` components — they are legal in a relative
/// import specifier.
pub fn normalize_relative_module_specifier(value: &str) -> String {
    let normalized = RelativePath::new(value).normalize().to_string();
    if normalized.is_empty() {
        ".".to_string()
    } else {
        normalized
    }
}

pub fn chunk_id_for_js_path(js_path: &str) -> Result<String> {
    let normalized = normalize_asset_path(js_path)?;
    Ok(normalized
        .strip_suffix(".js")
        .context("expected normalized .js path")?
        .to_string())
}

fn normalize_asset_path(path: &str) -> Result<String> {
    let normalized = normalize_module_path(&path.replace('\\', "/"))?;
    if !normalized.ends_with(".js") {
        bail!("Expected a .js path in JS list: {path}");
    }
    Ok(normalized)
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct JsListEntry {
    /// Path of the file to load; also determines the stable chunk ID.
    pub path: String,
    /// Original path used by imports inside the extracted source.
    pub source_path: String,
}

pub fn parse_js_list(text: &str) -> Result<Vec<JsListEntry>> {
    let mut out = Vec::new();
    let mut seen_paths = HashSet::new();
    let mut seen_source_paths = HashSet::new();
    for line in text.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        let (path, source_path) = match trimmed.split_once('\t') {
            Some((path, source_path)) if !source_path.contains('\t') => (path, source_path),
            Some(_) => bail!("JS list entry must have at most two tab-separated paths"),
            None => (trimmed, trimmed),
        };
        let path = normalize_asset_path(path)?;
        let source_path = normalize_asset_path(source_path)?;
        if !seen_paths.insert(path.clone()) {
            bail!("JS list contains duplicate paths: {path}");
        }
        if !seen_source_paths.insert(source_path.clone()) {
            bail!("JS list contains duplicate source paths: {source_path}");
        }
        out.push(JsListEntry { path, source_path });
    }
    Ok(out)
}

pub fn normalize_module_path(value: &str) -> Result<String> {
    if value.is_empty() || value.starts_with('/') {
        bail!("Expected a non-empty relative path");
    }
    let normalized = RelativePath::new(value).normalize();
    if normalized.as_str().is_empty() || normalized.components().any(|part| part.as_str() == "..") {
        bail!("Invalid relative path: {value}");
    }
    Ok(normalized.to_string())
}

pub fn path_from_module_path(path: &str) -> PathBuf {
    RelativePath::new(path).to_path("")
}

pub fn module_path_from_path(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

/// Render `target` as a path string for inclusion in a manifest serialized at
/// `manifest_path`. If `target` is under `manifest_path`'s parent, the result
/// is relative to that parent (so the manifest tree is portable). Otherwise
/// `target` is returned verbatim. The two paths must share an anchor —
/// either both absolute or both relative to the same cwd; mixing the two
/// produces a degenerate "no common prefix" result and `target` falls
/// through unchanged.
pub fn manifest_relative_path(manifest_path: &Path, target: &Path) -> String {
    let Some(manifest_dir) = manifest_path.parent() else {
        return module_path_from_path(target);
    };
    if let Ok(rel) = target.strip_prefix(manifest_dir) {
        if rel.as_os_str().is_empty() {
            return ".".to_string();
        }
        return module_path_from_path(rel);
    }
    module_path_from_path(target)
}

pub fn join_module_path(parts: &[&str]) -> String {
    parts
        .iter()
        .fold(relative_path::RelativePathBuf::new(), |base, part| {
            base.join_normalized(RelativePath::new(part))
        })
        .to_string()
}

pub fn module_path_dirname(path: &str) -> String {
    let path = path.replace('\\', "/");
    let normalized = RelativePath::new(&path).normalize();
    normalized
        .parent()
        .map(RelativePath::as_str)
        .unwrap_or("")
        .to_string()
}

pub(super) fn resolve_chunk_source_path_reference(
    source: &str,
    caller_source_path: &str,
) -> Option<String> {
    let imported_path = if source.starts_with('/') {
        normalize_module_path(source.trim_start_matches('/')).ok()?
    } else {
        normalize_module_path(&join_module_path(&[
            module_path_dirname(caller_source_path).as_str(),
            source,
        ]))
        .ok()?
    };
    imported_path.ends_with(".js").then_some(imported_path)
}
