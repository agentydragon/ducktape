//! Normalized output paths for materialized modules.

use anyhow::Result;
use artifact::paths::{join_module_path, normalize_module_path};

pub(super) fn target_file_for_request(target_dir: &str, target_path: &str) -> Result<String> {
    let normalized = normalize_module_path(target_path)?;
    let with_ext = if normalized.ends_with(".js") {
        normalized
    } else {
        format!("{normalized}.js")
    };
    Ok(join_module_path(&[target_dir, &with_ext]))
}

pub(super) fn normalize_optional_relative_dir(value: &str) -> Result<String> {
    if value.is_empty() {
        return Ok(String::new());
    }
    normalize_module_path(value)
}
