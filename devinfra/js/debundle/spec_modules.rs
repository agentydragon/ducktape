//! Shared helpers for walking a debundle spec's `modules/` directory.
//!
//! The main debundler (`spec_tree`) and the analysis CLIs
//! (`peel_horizon`, and forthcoming siblings) all need to:
//!
//! * Enumerate `*.yaml` (active) and `*.yaml.deferred` (parked)
//!   files under a spec's modules root.
//! * Deserialize each file's body via the canonical `ModuleFile`
//!   shape (members + anonymous statements).
//! * Ask path-level questions like "is this active or deferred?"
//!   and "what module path does this file represent?".
//!
//! This crate is the single source of truth for the above so the
//! debundler and the analysis tools always agree on the spec's
//! on-disk layout.

use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::Deserialize;

use spec::{AnonymousStatement, Member};

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ModuleFile {
    #[serde(default)]
    pub members: Vec<Member>,
    #[serde(default)]
    pub anonymous_statements: Vec<AnonymousStatement>,
}

pub fn is_module_yaml(path: &Path) -> bool {
    path.file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.ends_with(".yaml") || name.ends_with(".yaml.deferred"))
}

pub fn is_deferred_yaml(path: &Path) -> bool {
    path.file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.ends_with(".yaml.deferred"))
}

pub fn module_path_from_file(path: &Path, root: &Path, is_deferred: bool) -> String {
    let relative = path
        .strip_prefix(root)
        .unwrap_or(path)
        .to_string_lossy()
        .replace('\\', "/");
    let suffix = if is_deferred {
        ".yaml.deferred"
    } else {
        ".yaml"
    };
    relative
        .strip_suffix(suffix)
        .unwrap_or(&relative)
        .to_string()
}

pub fn collect_module_files(root: &Path) -> Result<Vec<PathBuf>> {
    let mut out = Vec::new();
    collect_module_files_into(root, &mut out)?;
    out.sort();
    Ok(out)
}

fn collect_module_files_into(root: &Path, out: &mut Vec<PathBuf>) -> Result<()> {
    for entry in fs::read_dir(root).with_context(|| format!("reading {}", root.display()))? {
        let path = entry
            .with_context(|| format!("walking {}", root.display()))?
            .path();
        if path.is_dir() {
            collect_module_files_into(&path, out)?;
        } else if is_module_yaml(&path) {
            out.push(path);
        }
    }
    Ok(())
}

pub fn read_module_file(path: &Path) -> Result<ModuleFile> {
    serde_yaml::from_str(
        &fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?,
    )
    .with_context(|| format!("parsing {}", path.display()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn write(root: &Path, rel: &str, body: &str) {
        let path = root.join(rel);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, body).unwrap();
    }

    #[test]
    fn collect_module_files_picks_yaml_and_deferred_skips_other_files() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(root, "ui/a.yaml", "members: []\n");
        write(root, "ui/b.yaml.deferred", "members: []\n");
        write(root, "ui/c.txt", "ignored\n");
        write(root, "ui/d.yaml.bak", "ignored\n");
        let files = collect_module_files(root).unwrap();
        let names: Vec<String> = files
            .iter()
            .map(|p| p.strip_prefix(root).unwrap().to_string_lossy().to_string())
            .collect();
        assert_eq!(names, vec!["ui/a.yaml", "ui/b.yaml.deferred"]);
    }

    #[test]
    fn module_path_from_file_strips_yaml_suffixes() {
        let root = Path::new("/spec");
        assert_eq!(
            module_path_from_file(Path::new("/spec/ui/list.yaml"), root, false),
            "ui/list",
        );
        assert_eq!(
            module_path_from_file(Path::new("/spec/ui/list.yaml.deferred"), root, true),
            "ui/list",
        );
    }

    #[test]
    fn read_module_file_round_trips_members_and_anonymous_statements() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("x.yaml");
        fs::write(
            &path,
            "members:\n  - selector: { binding: { name: a } }\nanonymous_statements: []\n",
        )
        .unwrap();
        let module = read_module_file(&path).unwrap();
        assert_eq!(module.members.len(), 1);
        assert_eq!(module.members[0].selector.binding.name, "a");
    }
}
