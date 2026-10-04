//! YAML helpers for spec-editing CLI commands.
//!
//! Edits are compared semantically; changed documents are reserialized in full.
//! Each replacement is atomic, but a multi-file command is not a transaction.

use std::fs;
use std::path::Path;

use anyhow::{Context, Result};
use serde_yaml::{Mapping, Value};
use tempfile::NamedTempFile;

pub fn read_yaml(path: &Path) -> Result<Value> {
    let text = fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
    let parsed: Value =
        serde_yaml::from_str(&text).with_context(|| format!("parsing {}", path.display()))?;
    Ok(empty_yaml_to_mapping(parsed))
}

fn yaml_semantically_changed(path: &Path, doc: &Value) -> Result<bool> {
    if !path.exists() {
        return Ok(true);
    }
    Ok(read_yaml(path)? != *doc)
}

/// Compare once, then optionally persist. Returns whether the edit changes YAML
/// semantics, including in dry-run mode. No-op edits preserve the original text.
pub fn apply_yaml_edit(path: &Path, doc: &Value, dry_run: bool) -> Result<bool> {
    let changed = yaml_semantically_changed(path, doc)?;
    if changed && !dry_run {
        write_yaml_atomic(path, doc)?;
    }
    Ok(changed)
}

/// Replace one file atomically, without comparing or interpreting its schema.
/// Callers decide whether the document changed and handle dry-run before calling.
pub fn write_yaml_atomic(path: &Path, doc: &Value) -> Result<()> {
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent).with_context(|| format!("creating {}", parent.display()))?;
    // A unique sibling stays on the same filesystem, never clobbers another
    // writer's scratch file, and is cleaned up automatically on failure.
    let mut temp = NamedTempFile::new_in(parent)
        .with_context(|| format!("creating temporary YAML beside {}", path.display()))?;
    serde_yaml::to_writer(temp.as_file_mut(), doc)
        .with_context(|| format!("serializing {}", path.display()))?;
    temp.persist(path)
        .with_context(|| format!("replacing {}", path.display()))?;
    Ok(())
}

fn empty_yaml_to_mapping(value: Value) -> Value {
    match value {
        Value::Null => Value::Mapping(Mapping::new()),
        other => other,
    }
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::Path;

    use serde_yaml::Value;
    use tempfile::TempDir;

    use super::{apply_yaml_edit, read_yaml};

    fn write(root: &Path, rel: &str, body: &str) {
        let path = root.join(rel);
        fs::write(path, body).unwrap();
    }

    #[test]
    fn formatting_only_difference_is_not_semantic_change() {
        let dir = TempDir::new().unwrap();
        let root = dir.path();
        write(
            root,
            "m.yaml",
            "# keep me\nmembers: [ { selector: { binding: { name: a } } } ]\n",
        );
        let path = root.join("m.yaml");
        let doc = read_yaml(&path).unwrap();

        assert!(!apply_yaml_edit(&path, &doc, false).unwrap());
        assert_eq!(
            fs::read_to_string(path).unwrap(),
            "# keep me\nmembers: [ { selector: { binding: { name: a } } } ]\n"
        );
    }

    #[test]
    fn dry_run_reports_change_without_creating_directories() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("nested/m.yaml");
        let doc: Value = serde_yaml::from_str("members: []").unwrap();
        assert!(apply_yaml_edit(&path, &doc, true).unwrap());
        assert!(!path.parent().unwrap().exists());
        assert!(apply_yaml_edit(&path, &doc, false).unwrap());
        assert_eq!(read_yaml(&path).unwrap(), doc);
        assert!(!apply_yaml_edit(&path, &doc, false).unwrap());
    }
}
