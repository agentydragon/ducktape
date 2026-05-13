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

use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::Deserialize;

use spec::{AnonymousStatement, BindingSourceKind, Member};

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

/// Module path components that hold the spec's residual catch-all
/// (default emit target `residual/unhandled` per
/// `spec::ResidualModule`). Files under any directory whose
/// top-level segment is `residual/` are treated as "the still-to-be-
/// factorized pile" — their bindings are NOT considered claimed.
///
/// Detection is by module-path prefix only (matches both the
/// `residual/unhandled.yaml.deferred` default and any spec author's
/// custom `residual/<other>.yaml{,.deferred}`). The factorizer reads
/// this to scope its residual graph; `spec_tree`'s materializer
/// reads the active `ResidualModule` config independently and is
/// unaffected.
pub fn is_residual_module_path(module_path: &str) -> bool {
    module_path == "residual" || module_path.starts_with("residual/")
}

/// Every chunk-top binding name claimed by an `*.yaml` or
/// `*.yaml.deferred` member's `selector.binding.name`. Excludes:
///
/// * Members whose binding kind is `ImportSpecifier` (those refer
///   to upstream symbols, not chunk-local bindings).
/// * Files under any `residual/` directory — those bindings are
///   the catch-all "not yet factored" pile and downstream tools
///   (the factorizer) need to see them as in-scope, not claimed.
///
/// A binding being "claimed" means: the spec has assigned it to
/// some non-residual module file, either active or deferred. The
/// factorizer treats every claimed binding as out-of-scope — even
/// deferred claims — because deferred modules are "owned, hands
/// off" until promoted or peeled.
pub fn load_claimed_bindings(modules_root: &Path) -> Result<BTreeSet<String>> {
    let mut claimed = BTreeSet::new();
    for path in collect_module_files(modules_root)? {
        let module_path = module_path_from_file(&path, modules_root, is_deferred_yaml(&path));
        if is_residual_module_path(&module_path) {
            continue;
        }
        for member in read_module_file(&path)?.members {
            let binding = member.selector.binding;
            if matches!(binding.kind, Some(BindingSourceKind::ImportSpecifier)) {
                continue;
            }
            claimed.insert(binding.name);
        }
    }
    Ok(claimed)
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

    #[test]
    fn is_residual_module_path_matches_residual_subtree_only() {
        assert!(is_residual_module_path("residual"));
        assert!(is_residual_module_path("residual/unhandled"));
        assert!(is_residual_module_path("residual/custom/sub"));
        assert!(!is_residual_module_path("ui/sidebar"));
        // The substring "residual" inside a path segment shouldn't
        // match — only the top-level segment counts.
        assert!(!is_residual_module_path("ui/residual"));
        assert!(!is_residual_module_path("residualish/foo"));
    }

    #[test]
    fn load_claimed_bindings_collects_active_and_deferred_skips_imports_and_residual() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(
            root,
            "ui/active.yaml",
            "members:\n  - selector: { binding: { name: a } }\n",
        );
        write(
            root,
            "ui/deferred.yaml.deferred",
            "members:\n  - selector: { binding: { name: b } }\n",
        );
        write(
            root,
            "ui/import.yaml",
            "members:\n  - selector: { binding: { name: c, kind: import_specifier } }\n",
        );
        write(
            root,
            "residual/unhandled.yaml.deferred",
            "members:\n  - selector: { binding: { name: d } }\n",
        );
        let claimed = load_claimed_bindings(root).unwrap();
        assert_eq!(claimed, BTreeSet::from(["a".to_string(), "b".to_string()]));
    }
}
