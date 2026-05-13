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

use std::collections::{BTreeMap, BTreeSet};
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
/// factorized pile". Detection is by module-path prefix only
/// (matches both the `residual/unhandled.yaml.deferred` default and
/// any spec author's custom `residual/<other>.yaml{,.deferred}`).
pub fn is_residual_module_path(module_path: &str) -> bool {
    module_path == "residual" || module_path.starts_with("residual/")
}

/// Every chunk-top binding name claimed by an *active* `*.yaml`
/// file's `selector.binding.name`, mapped to the module path that
/// owns it. **Deferred files (`*.yaml.deferred`) are NOT included**
/// because deferred members get their `name:` applied as a chunk-top
/// rename but stay physically in `residual_entry` — they don't
/// materialize as separate modules. The factorizer needs to see
/// them as in-scope candidates for clustering, not as immovable
/// claims.
///
/// Excludes:
///
/// * Members whose binding kind is `ImportSpecifier` (those refer
///   to upstream symbols, not chunk-local bindings).
/// * Files under any `residual/` directory (catch-all module that
///   doesn't represent a permanent home).
///
/// Returned map's values are the module path (no `.yaml` extension)
/// the binding lives in.
pub fn load_active_claims(modules_root: &Path) -> Result<BTreeMap<String, String>> {
    let mut claims = BTreeMap::new();
    for path in collect_module_files(modules_root)? {
        if is_deferred_yaml(&path) {
            continue;
        }
        let module_path = module_path_from_file(&path, modules_root, false);
        if is_residual_module_path(&module_path) {
            continue;
        }
        for member in read_module_file(&path)?.members {
            let binding = member.selector.binding;
            if matches!(binding.kind, Some(BindingSourceKind::ImportSpecifier)) {
                continue;
            }
            claims.insert(binding.name, module_path.clone());
        }
    }
    Ok(claims)
}

/// Every binding name grouped by deferred module
/// (`*.yaml.deferred`, excluding `residual/`). The factorizer
/// treats each entry as a **seed group**: bindings in the same
/// deferred module are forced into the same proposed cell —
/// preexisting factors can grow (absorb residual bindings) or
/// merge with other cells, but they can't be split apart.
///
/// The map's value (binding -> module path) is the inverse
/// mapping useful for "which deferred module did this binding
/// come from?"; the caller can build path -> {bindings} by
/// grouping if needed.
pub fn load_deferred_groups(modules_root: &Path) -> Result<BTreeMap<String, String>> {
    let mut groups = BTreeMap::new();
    for path in collect_module_files(modules_root)? {
        if !is_deferred_yaml(&path) {
            continue;
        }
        let module_path = module_path_from_file(&path, modules_root, true);
        if is_residual_module_path(&module_path) {
            continue;
        }
        for member in read_module_file(&path)?.members {
            let binding = member.selector.binding;
            if matches!(binding.kind, Some(BindingSourceKind::ImportSpecifier)) {
                continue;
            }
            groups.insert(binding.name, module_path.clone());
        }
    }
    Ok(groups)
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
    fn load_deferred_groups_returns_only_deferred_bindings_with_module_paths() {
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
            "members:\n  - selector: { binding: { name: b } }\n  - selector: { binding: { name: c } }\n",
        );
        write(
            root,
            "residual/unhandled.yaml.deferred",
            "members:\n  - selector: { binding: { name: d } }\n",
        );
        let groups = load_deferred_groups(root).unwrap();
        let names: BTreeSet<String> = groups.keys().cloned().collect();
        assert_eq!(names, BTreeSet::from(["b".to_string(), "c".to_string()]));
        assert_eq!(groups.get("b"), Some(&"ui/deferred".to_string()));
        assert_eq!(groups.get("c"), Some(&"ui/deferred".to_string()));
    }

    #[test]
    fn load_active_claims_returns_only_active_yaml_bindings_with_module_paths() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        // Active: claimed.
        write(
            root,
            "ui/active.yaml",
            "members:\n  - selector: { binding: { name: a } }\n",
        );
        // Deferred: NOT claimed — deferred modules don't materialize,
        // their members are still physically in residual_entry.
        write(
            root,
            "ui/deferred.yaml.deferred",
            "members:\n  - selector: { binding: { name: b } }\n",
        );
        // ImportSpecifier: skipped (upstream symbol).
        write(
            root,
            "ui/import.yaml",
            "members:\n  - selector: { binding: { name: c, kind: import_specifier } }\n",
        );
        // Residual catch-all: skipped.
        write(
            root,
            "residual/unhandled.yaml.deferred",
            "members:\n  - selector: { binding: { name: d } }\n",
        );
        let claims = load_active_claims(root).unwrap();
        let claimed_names: BTreeSet<String> = claims.keys().cloned().collect();
        assert_eq!(claimed_names, BTreeSet::from(["a".to_string()]));
        assert_eq!(claims.get("a"), Some(&"ui/active".to_string()));
    }
}
