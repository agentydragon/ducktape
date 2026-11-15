//! Exact filesystem observations, independent of report normalization.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

/// Read all regular fixture files, keyed by their path relative to `root`.
pub fn read_tree(root: &Path) -> BTreeMap<PathBuf, Vec<u8>> {
    let mut files = BTreeMap::new();
    let mut pending = vec![root.to_path_buf()];
    while let Some(dir) = pending.pop() {
        for entry in fs::read_dir(&dir).unwrap_or_else(|e| panic!("read {}: {e}", dir.display())) {
            let path = entry.expect("directory entry").path();
            if path.is_dir() {
                pending.push(path);
            } else {
                let bytes =
                    fs::read(&path).unwrap_or_else(|e| panic!("read {}: {e}", path.display()));
                files.insert(path.strip_prefix(root).unwrap().to_path_buf(), bytes);
            }
        }
    }
    files
}

/// Both trees contain exactly the same relative paths and bytes.
pub fn assert_same_tree(left: &Path, right: &Path) {
    let (left, right) = (read_tree(left), read_tree(right));
    assert_eq!(
        left.keys().collect::<Vec<_>>(),
        right.keys().collect::<Vec<_>>(),
        "emitted file sets differ"
    );
    for (file, bytes) in &left {
        assert!(right[file] == *bytes, "{} differs", file.display());
    }
}
