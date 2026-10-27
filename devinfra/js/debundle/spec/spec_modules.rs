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
//! * Deserialize flat transform specs through their canonical typed schema.
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

use spec::{
    AnonymousStatementSelector, BindingSourceKind, Member, MemberSelectorSpec, ModulePath,
    SourceMatchClaim, TransformSpec, is_residual_namespace_path,
};

/// The on-disk module and the logical module share one canonical schema.
pub type ModuleFile = spec::LogicalModule;

/// Consolidate byte-identical source-match templates with identical metadata.
/// Keep the first record's position and append captures in authoring order.
/// Different selector notes or overlapping local captures remain separate;
/// in particular, two export aliases of one local must not be collapsed.
/// Callers must resolve and validate the resulting document before writing it.
pub fn coalesce_source_match_claims(module: &mut ModuleFile) {
    let mut grouped: Vec<SourceMatchClaim> = Vec::new();
    for claim in std::mem::take(&mut module.source_matches) {
        // Exhaustive destructuring makes new metadata fields require an explicit
        // decision here instead of silently disappearing from the grouping key.
        let SourceMatchClaim {
            identifiers,
            match_source,
            bindings,
            note,
        } = claim;
        if let Some(previous) = grouped.iter_mut().find(|previous| {
            !bindings.is_empty()
                && !previous.bindings.is_empty()
                && previous.identifiers == identifiers
                && previous.match_source == match_source
                && previous.note == note
                && !bindings.iter().any(|binding| {
                    previous.bindings.iter().any(|existing| {
                        existing.local() == binding.local() || existing.name() == binding.name()
                    })
                })
        }) {
            previous.bindings.extend(bindings);
        } else {
            grouped.push(SourceMatchClaim {
                identifiers,
                match_source,
                bindings,
                note,
            });
        }
    }
    module.source_matches = grouped;
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct BindingPatchesFile {
    #[serde(default)]
    pub members: Vec<Member>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ModuleClaims {
    pub bindings: BTreeSet<String>,
    /// Members retained for joint selector resolution. Binding pins also serve
    /// as anchors for relational selectors in other modules.
    pub member_selectors: Vec<(String, MemberSelectorSpec)>,
    pub anonymous_selectors: BTreeSet<AnonymousStatementSelector>,
    /// Canonical grouped source-match entries. Expanded by
    /// `source_match::source_match_claim_member_selectors` by consumers
    /// that need per-binding selectors.
    pub source_matches: Vec<SourceMatchClaim>,
}

impl ModuleClaims {
    pub fn is_empty(&self) -> bool {
        self.bindings.is_empty()
            && self.member_selectors.is_empty()
            && self.anonymous_selectors.is_empty()
            && self.source_matches.is_empty()
    }

    pub fn extend(&mut self, other: ModuleClaims) {
        self.bindings.extend(other.bindings);
        self.member_selectors.extend(other.member_selectors);
        self.anonymous_selectors.extend(other.anonymous_selectors);
        self.source_matches.extend(other.source_matches);
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

/// Read a flat transform spec with the same typed schema used by the pipeline.
pub fn read_flat_transform_spec(path: &Path) -> Result<TransformSpec> {
    serde_yaml::from_slice(&fs::read(path).with_context(|| format!("reading {}", path.display()))?)
        .with_context(|| format!("parsing {}", path.display()))
}

fn read_binding_patches_file(path: &Path) -> Result<BindingPatchesFile> {
    serde_yaml::from_str(
        &fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?,
    )
    .with_context(|| format!("parsing {}", path.display()))
}

pub fn module_claims(module: ModuleFile) -> Result<ModuleClaims> {
    let mut claims = ModuleClaims::default();
    for member in module.members {
        let selector = member.selector.selected()?;
        let name = match (&member.name, &selector) {
            (Some(name), _) => name.clone(),
            (None, MemberSelectorSpec::Binding(binding)) => binding.name.clone(),
            (None, _) => anyhow::bail!(
                "member using {} requires name",
                selector.selector_kind_label()
            ),
        };
        if let MemberSelectorSpec::Binding(binding) = &selector {
            claims.bindings.insert(binding.name.clone());
        }
        claims.member_selectors.push((name, selector));
    }
    claims.source_matches = module.source_matches;
    for statement in module.anonymous_statements {
        claims.anonymous_selectors.insert(statement.selector()?);
    }
    Ok(claims)
}

pub fn read_module_claims(path: &Path) -> Result<ModuleClaims> {
    if !is_module_yaml(path) {
        return Ok(ModuleClaims::default());
    }
    module_claims(read_module_file(path)?)
}

pub fn load_binding_patch_members(modules_root: &Path) -> Result<Vec<Member>> {
    let path = default_binding_patches_path(modules_root);
    if !path.exists() {
        return Ok(Vec::new());
    }
    let members = read_binding_patches_file(&path)?.members;
    for (index, member) in members.iter().enumerate() {
        let selector = member.selector.selected().with_context(|| {
            format!(
                "{}: member {index} requires exactly one binding selector",
                path.display()
            )
        })?;
        anyhow::ensure!(
            matches!(selector, MemberSelectorSpec::Binding(_)),
            "{}: member {index} requires a binding selector",
            path.display(),
        );
    }
    Ok(members)
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
        if is_residual_namespace_path(&raw_path) {
            continue;
        }
        let module_path = ModulePath::parse(&raw_path, "")
            .with_context(|| format!("module path from {}", path.display()))?;
        for member in read_module_file(&path)?.members {
            let Some(binding) = member.selector.binding else {
                continue;
            };
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
    fn coalescing_keeps_incompatible_capture_aliases_and_identifier_modes_separate() {
        // Multiple aliases of one capture cannot form one grouped claim. This
        // document-level corner is deliberately tested before source validation.
        let mut module: ModuleFile = serde_yaml::from_str(
            "source_matches:\n\
             - {match: 'const local = 1;', bindings: [{local: local, name: First}]}\n\
             - {match: 'const local = 1;', bindings: [{local: local, name: Second}]}\n\
             - {match: 'const local = 1;', bindings: [other]}\n\
             - {match: 'const local = 1;', bindings: []}\n",
        )
        .unwrap();
        module.source_matches[2].identifiers = spec::SourceMatchIdentifierMode::Exact;
        let original = module.clone();
        coalesce_source_match_claims(&mut module);
        assert_eq!(module.source_matches, original.source_matches);
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
    fn read_module_claims_includes_anonymous_selectors() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("x.yaml");
        fs::write(
            &path,
            r#"members:
  - selector: { binding: { name: Co } }
anonymous_statements:
  - match: 'decorate(Co);'
    note: "decorator on Co"
  - source_match:
      match: 'register(Co);'
    comment: |
      Registers Co before registry consumers run.
"#,
        )
        .unwrap();

        let claims = read_module_claims(&path).unwrap();
        assert_eq!(claims.bindings, BTreeSet::from(["Co".to_string()]));
        assert_eq!(
            claims.anonymous_selectors,
            BTreeSet::from([
                AnonymousStatementSelector::exact("decorate(Co);"),
                AnonymousStatementSelector {
                    match_source: "register(Co);".to_string(),
                    identifiers: spec::SourceMatchIdentifierMode::AlphaAll,
                    target_binding: None,
                },
            ])
        );
    }

    #[test]
    fn read_module_file_rejects_unknown_field_with_the_file_path_in_its_context() {
        // `ModuleFile` and the nested `AnonymousStatement` both carry
        // `#[serde(deny_unknown_fields)]`, so a typo on an entry is an error.
        // The outermost context is `read_module_file`'s own `parsing <path>`.
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

        assert!(
            err.to_string().contains(&path.display().to_string()),
            "outermost context should name the offending file, got: {err:#}",
        );
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
