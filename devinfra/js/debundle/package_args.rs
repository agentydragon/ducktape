//! Shared command-line package-root parsing.

use anyhow::Result;
use std::path::PathBuf;

pub fn parse_package_root_kv(value: &str) -> Result<(String, PathBuf), String> {
    let Some(separator) = value.find('=') else {
        return Err(format!(
            "package root mapping must be <package>=<dir>, got {value}"
        ));
    };
    if separator == 0 || separator == value.len() - 1 {
        return Err(format!(
            "package root mapping must be <package>=<dir>, got {value}"
        ));
    }
    Ok((
        value[..separator].to_string(),
        PathBuf::from(&value[separator + 1..]),
    ))
}

pub fn collect_package_roots(
    pairs: impl IntoIterator<Item = (String, PathBuf)>,
) -> Result<std::collections::BTreeMap<String, PathBuf>> {
    let mut roots = std::collections::BTreeMap::new();
    for (name, path) in pairs {
        anyhow::ensure!(
            roots.insert(name.clone(), path).is_none(),
            "duplicate package root mapping for {name}"
        );
    }
    Ok(roots)
}
