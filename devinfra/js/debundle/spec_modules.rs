//! Shared helpers for walking a debundle spec's authoring files.
//!
//! The main debundler (`spec_tree`) and the peel-planning CLI all need to:
//!
//! * Enumerate emitted-module `*.yaml` files under a spec's
//!   `modules/` root.
//! * Deserialize the spec-root `binding_patches.yaml` stream for
//!   non-emitting binding edits.
//! * Deserialize each file's body via the canonical `ModuleFile`
//!   shape (members + anonymous statements).
//! * Ask path-level questions like "what module path does this file
//!   represent?".
//!
//! This crate is the single source of truth for the above so the
//! debundler and the analysis tools always agree on the spec's
//! on-disk layout.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::Deserialize;

use spec::{AnonymousStatement, BindingSourceKind, Member, ModulePath};

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ModuleFile {
    /// Optional module-level human-readable comment. Emitted at the
    /// top of the generated module file, before any imports. See
    /// [`spec::LogicalModule::comment`].
    #[serde(default)]
    pub comment: Option<String>,
    #[serde(default)]
    pub members: Vec<Member>,
    #[serde(default)]
    pub anonymous_statements: Vec<AnonymousStatement>,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BindingPatchesFile {
    #[serde(default)]
    pub members: Vec<Member>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ModuleClaims {
    pub bindings: BTreeSet<String>,
    pub anonymous_match_sources: BTreeSet<String>,
}

impl ModuleClaims {
    pub fn is_empty(&self) -> bool {
        self.bindings.is_empty() && self.anonymous_match_sources.is_empty()
    }

    pub fn has_claims(&self) -> bool {
        !self.bindings.is_empty() || !self.anonymous_match_sources.is_empty()
    }

    pub fn extend(&mut self, other: ModuleClaims) {
        self.bindings.extend(other.bindings);
        self.anonymous_match_sources
            .extend(other.anonymous_match_sources);
    }
}

pub fn is_module_yaml(path: &Path) -> bool {
    path.file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.ends_with(".yaml"))
}

pub fn module_path_from_file(path: &Path, root: &Path) -> String {
    let relative = path
        .strip_prefix(root)
        .unwrap_or(path)
        .to_string_lossy()
        .replace('\\', "/");
    relative
        .strip_suffix(".yaml")
        .unwrap_or(&relative)
        .to_string()
}

pub fn default_binding_patches_path(modules_root: &Path) -> PathBuf {
    modules_root
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .join("binding_patches.yaml")
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

pub fn read_binding_patches_file(path: &Path) -> Result<BindingPatchesFile> {
    serde_yaml::from_str(
        &fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?,
    )
    .with_context(|| format!("parsing {}", path.display()))
}

pub fn module_claims(module: ModuleFile) -> ModuleClaims {
    let mut claims = ModuleClaims::default();
    for member in module.members {
        claims.bindings.insert(member.selector.binding.name);
    }
    for statement in module.anonymous_statements {
        claims
            .anonymous_match_sources
            .insert(statement.match_source);
    }
    claims
}

pub fn read_module_claims(path: &Path) -> Result<ModuleClaims> {
    if !is_module_yaml(path) {
        return Ok(ModuleClaims::default());
    }
    Ok(module_claims(read_module_file(path)?))
}

pub fn load_binding_patch_members(modules_root: &Path) -> Result<Vec<Member>> {
    let path = default_binding_patches_path(modules_root);
    if !path.exists() {
        return Ok(Vec::new());
    }
    Ok(read_binding_patches_file(&path)?.members)
}

pub fn load_binding_patch_bindings(modules_root: &Path) -> Result<BTreeSet<String>> {
    let mut bindings = BTreeSet::new();
    for member in load_binding_patch_members(modules_root)? {
        let binding = member.selector.binding;
        if matches!(binding.kind, Some(BindingSourceKind::ImportSpecifier)) {
            continue;
        }
        bindings.insert(binding.name);
    }
    Ok(bindings)
}

/// Module path components that hold the spec's residual catch-all
/// (default emit target [`spec::DEFAULT_RESIDUAL_MODULE_PATH`]
/// per `spec::UnassignedMode::CatchallFile`). Files under any
/// directory whose top-level segment is `residual/` are treated as
/// "the still-to-be-factorized pile". Detection is by module-path
/// prefix only (matches both the default and any spec author's
/// custom `residual/<other>.yaml`).
pub fn is_residual_module_path(module_path: &str) -> bool {
    module_path == "residual" || module_path.starts_with("residual/")
}

/// Every chunk-top binding name claimed by an emitted `*.yaml`
/// file's `selector.binding.name`, mapped to the module path that
/// owns it. Binding patches are not included because they don't
/// materialize as modules.
///
/// Excludes:
///
/// * Members whose binding kind is `ImportSpecifier` (those refer
///   to upstream symbols, not chunk-local bindings).
/// * Files under any `residual/` directory (catch-all module that
///   doesn't represent a permanent home).
///
/// Returned map's values are the canonical [`ModulePath`] (the
/// `*.yaml` file location with the extension stripped) the binding
/// lives in. The path comes from an on-disk file location, so it is
/// already the clean spelling — no chunk-id context applies.
pub fn load_active_claims(modules_root: &Path) -> Result<BTreeMap<String, ModulePath>> {
    let mut claims = BTreeMap::new();
    for path in collect_module_files(modules_root)? {
        let raw_path = module_path_from_file(&path, modules_root);
        if is_residual_module_path(&raw_path) {
            continue;
        }
        let module_path = ModulePath::parse(&raw_path, "")
            .with_context(|| format!("module path from {}", path.display()))?;
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeSet;
    use tempfile::TempDir;

    fn write(root: &Path, rel: &str, body: &str) {
        let path = root.join(rel);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, body).unwrap();
    }

    #[test]
    fn collect_module_files_picks_yaml_and_skips_other_files() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(root, "ui/a.yaml", "members: []\n");
        write(root, "ui/c.txt", "ignored\n");
        write(root, "ui/d.yaml.bak", "ignored\n");
        let files = collect_module_files(root).unwrap();
        let names: Vec<String> = files
            .iter()
            .map(|p| p.strip_prefix(root).unwrap().to_string_lossy().to_string())
            .collect();
        assert_eq!(names, vec!["ui/a.yaml"]);
    }

    #[test]
    fn module_path_from_file_strips_yaml_suffix() {
        let root = Path::new("/spec");
        assert_eq!(
            module_path_from_file(Path::new("/spec/ui/list.yaml"), root),
            "ui/list",
        );
    }

    #[test]
    fn read_module_file_round_trips_members_and_anonymous_statements() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("x.yaml");
        fs::write(
            &path,
            r#"members:
  - selector: { binding: { name: a } }
anonymous_statements: []
"#,
        )
        .unwrap();
        let module = read_module_file(&path).unwrap();
        assert_eq!(module.members.len(), 1);
        assert_eq!(module.members[0].selector.binding.name, "a");
    }

    #[test]
    fn read_module_claims_includes_anonymous_match_sources() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("x.yaml");
        fs::write(
            &path,
            r#"members:
  - selector: { binding: { name: Co } }
anonymous_statements:
  - match: 'decorate(Co);'
    note: "decorator on Co"
  - match: 'register(Co);'
    comment: |
      Registers Co before registry consumers run.
"#,
        )
        .unwrap();

        let claims = read_module_claims(&path).unwrap();
        assert_eq!(claims.bindings, BTreeSet::from(["Co".to_string()]));
        assert_eq!(
            claims.anonymous_match_sources,
            BTreeSet::from(["decorate(Co);".to_string(), "register(Co);".to_string()])
        );
    }

    #[test]
    fn read_module_file_round_trips_comment_fields_on_module_and_member() {
        // Module-level `comment:` and per-member `comment:` both
        // deserialize via the optional `Option<String>` fields. The
        // lowering pass renders them as JS `// ...` blocks; here we
        // only assert the parser round-trips the YAML shape.
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("x.yaml");
        fs::write(
            &path,
            r#"comment: |
  Module overview
  spans two lines.
members:
  - selector: { binding: { name: a } }
    comment: |
      Per-member comment
      across two lines.
"#,
        )
        .unwrap();
        let module = read_module_file(&path).unwrap();
        assert_eq!(
            module.comment.as_deref(),
            Some("Module overview\nspans two lines.\n"),
        );
        assert_eq!(module.members.len(), 1);
        assert_eq!(
            module.members[0].comment.as_deref(),
            Some("Per-member comment\nacross two lines.\n"),
        );
    }

    #[test]
    fn read_module_file_accepts_comment_field_on_anonymous_statement() {
        // `AnonymousStatement` accepts `comment:` alongside `note:`
        // (both `Option<String>`). `comment:` is the JS-visible prose
        // field; `note:` is YAML-only scratch metadata.
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("x.yaml");
        fs::write(
            &path,
            r#"anonymous_statements:
  - match: "foo();"
    comment: |
      Registers foo before registry consumers run.
  - match: "bar();"
    note: one-liner note
"#,
        )
        .unwrap();
        let module = read_module_file(&path).unwrap();
        assert_eq!(module.anonymous_statements.len(), 2);
        assert_eq!(
            module.anonymous_statements[0].comment.as_deref(),
            Some("Registers foo before registry consumers run.\n"),
        );
        assert!(module.anonymous_statements[0].note.is_none());
        assert_eq!(
            module.anonymous_statements[1].note.as_deref(),
            Some("one-liner note"),
        );
        assert!(module.anonymous_statements[1].comment.is_none());
    }

    #[test]
    fn read_module_file_unknown_field_error_names_the_field_and_file() {
        // `ModuleFile` and the nested `AnonymousStatement` both carry
        // `#[serde(deny_unknown_fields)]`, so a typo on an entry must
        // surface in the error. The `with_context` wrapper at the
        // call site adds the file path on top, and `anyhow::Error`'s
        // Display alternate (`{:#}`) flattens the whole chain.
        // Without the alternate formatter the CLI silently drops
        // everything below `parsing <path>`.
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("bad.yaml");
        fs::write(
            &path,
            r#"anonymous_statements:
  - match: "foo();"
    bogus_field: oops
"#,
        )
        .unwrap();

        let err = read_module_file(&path).expect_err("must reject unknown field");
        let msg = format!("{err:#}");

        assert!(
            msg.contains("bogus_field"),
            "error should name the bad field, got: {msg}",
        );
        assert!(
            msg.contains("unknown field") || msg.contains("expected one of"),
            "error should explain the deny_unknown_fields rejection, got: {msg}",
        );
        assert!(
            msg.contains("bad.yaml"),
            "error should include the offending file path, got: {msg}",
        );
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
    fn load_binding_patch_bindings_reads_spec_root_patch_file() {
        let dir = TempDir::new().unwrap();
        let root = dir.path().join("modules");
        write(
            dir.path(),
            "binding_patches.yaml",
            "members:\n  - selector: { binding: { name: b } }\n  - selector: { binding: { name: c } }\n",
        );

        let names = load_binding_patch_bindings(&root).unwrap();

        assert_eq!(names, BTreeSet::from(["b".to_string(), "c".to_string()]));
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
        // Patch backup: ignored and not claimed.
        write(
            root,
            "ui/patches.yaml.bak",
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
            "residual/unhandled.yaml",
            "members:\n  - selector: { binding: { name: d } }\n",
        );
        let claims = load_active_claims(root).unwrap();
        let claimed_names: BTreeSet<String> = claims.keys().cloned().collect();
        assert_eq!(claimed_names, BTreeSet::from(["a".to_string()]));
        assert_eq!(
            claims.get("a"),
            Some(&ModulePath::parse("ui/active", "").unwrap()),
        );
    }
}
