//! Atomic YAML writer for spec-editing CLI commands.
//!
//! Callers parse spec files through their typed schema before editing. A
//! multi-file command is not a transaction.

use std::fs;
use std::path::Path;

use anyhow::{Context, Result};
use serde_yaml::Value;
use tempfile::NamedTempFile;

/// Replace one file atomically. Callers decide whether the typed document
/// changed and handle dry-run before calling.
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

#[cfg(test)]
mod tests {
    use serde_yaml::Value;
    use std::fs;
    use tempfile::TempDir;

    use super::write_yaml_atomic;

    #[test]
    fn atomic_writer_replaces_one_file() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("m.yaml");
        fs::write(&path, "members: []\n").unwrap();
        let doc: Value =
            serde_yaml::from_str("members: [{selector: {binding: {name: a}}}]").unwrap();

        write_yaml_atomic(&path, &doc).unwrap();
        let parsed: Value = serde_yaml::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(parsed, doc);
    }

    #[test]
    fn atomic_writer_creates_parent_directories() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("nested/m.yaml");
        let doc: Value = serde_yaml::from_str("members: []").unwrap();
        assert!(!path.parent().unwrap().exists());
        write_yaml_atomic(&path, &doc).unwrap();
        let parsed: Value = serde_yaml::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(parsed, doc);
    }
}
