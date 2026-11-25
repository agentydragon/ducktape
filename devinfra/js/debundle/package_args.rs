//! Shared command-line package-root parsing.

use anyhow::Result;
use std::path::PathBuf;

/// Package sources used by the transform, projection and analyzer commands.
#[derive(clap::Args, Debug, Clone, Default)]
pub struct PackageRootArgs {
    /// Map a package name to its source directory: `<pkg>=<dir>`. May be repeated.
    #[arg(long = "package-root", value_parser = parse_package_root_kv)]
    pub package_roots: Vec<(String, PathBuf)>,
    /// Root directory containing per-package sources (alternative to repeated --package-root).
    #[arg(long)]
    pub packages_root: Option<PathBuf>,
}

pub fn parse_package_root_kv(value: &str) -> Result<(String, PathBuf), String> {
    let (package, directory) = value
        .split_once('=')
        .filter(|(package, directory)| !package.is_empty() && !directory.is_empty())
        .ok_or_else(|| format!("package root mapping must be <package>=<dir>, got {value}"))?;
    Ok((package.to_owned(), PathBuf::from(directory)))
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
