//! YAML helpers for spec-editing CLI commands.
//!
//! These commands preserve author formatting and comments when the requested
//! edit does not change the parsed YAML structure. Once the structure changes,
//! we still serialize with `serde_yaml`, matching the existing CLI behavior.

use std::fs;
use std::path::Path;

use anyhow::{Context, Result};
use serde_yaml::{Mapping, Value};

pub fn read_yaml(path: &Path) -> Result<Value> {
    let text = fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
    let parsed: Value =
        serde_yaml::from_str(&text).with_context(|| format!("parsing {}", path.display()))?;
    Ok(empty_yaml_to_mapping(parsed))
}

pub fn yaml_semantically_changed(path: &Path, doc: &Value) -> Result<bool> {
    if !path.exists() {
        return Ok(true);
    }
    Ok(read_yaml(path)? != *doc)
}

pub fn write_yaml_if_semantic_changed(path: &Path, doc: &Value) -> Result<bool> {
    let body =
        serde_yaml::to_string(doc).with_context(|| format!("serializing {}", path.display()))?;
    write_yaml_body_if_semantic_changed(path, doc, body)
}

pub fn write_yaml_body_if_semantic_changed(path: &Path, doc: &Value, body: String) -> Result<bool> {
    if !yaml_semantically_changed(path, doc)? {
        return Ok(false);
    }
    fs::write(path, body).with_context(|| format!("writing {}", path.display()))?;
    Ok(true)
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

    use serde_yaml::{Mapping, Value};
    use tempfile::TempDir;

    use super::{read_yaml, write_yaml_if_semantic_changed, yaml_semantically_changed};

    fn yk(s: &str) -> Value {
        Value::String(s.to_string())
    }

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

        assert!(!yaml_semantically_changed(&path, &doc).unwrap());
        assert!(!write_yaml_if_semantic_changed(&path, &doc).unwrap());
        assert_eq!(
            fs::read_to_string(path).unwrap(),
            "# keep me\nmembers: [ { selector: { binding: { name: a } } } ]\n"
        );
    }

    #[test]
    fn missing_file_is_semantic_change() {
        let dir = TempDir::new().unwrap();
        let mut map = Mapping::new();
        map.insert(yk("members"), Value::Sequence(Vec::new()));
        assert!(
            yaml_semantically_changed(&dir.path().join("new.yaml"), &Value::Mapping(map)).unwrap()
        );
    }
}
