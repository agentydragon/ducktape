//! One lifetime for a case's inputs, per-invocation outputs and failure evidence.

use super::current_test_prefix;
use serde::{Deserialize, Serialize};
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use tempfile::TempDir;

static CASE_COUNTER: AtomicUsize = AtomicUsize::new(0);

#[derive(Deserialize, Serialize)]
pub(super) struct CaseMetadata {
    pub original_root: PathBuf,
    pub test: String,
}

struct CaseRoot {
    directory: TempDir,
    destination: Option<PathBuf>,
    retained: AtomicBool,
}

/// Clones and child output directories keep the same case alive. An unwinding
/// handle retains the entire case before any temporary directory is removed.
#[derive(Clone)]
pub struct CaseWorkspace {
    root: Arc<CaseRoot>,
    directory: PathBuf,
}

impl Default for CaseWorkspace {
    fn default() -> Self {
        let outputs = std::env::var_os("TEST_UNDECLARED_OUTPUTS_DIR")
            .map(PathBuf::from)
            .map(|path| path.join("failed-fixtures"));
        Self::with_retention_root(outputs.as_deref())
    }
}

impl CaseWorkspace {
    /// An explicit destination also permits testing retention without changing
    /// the process environment shared by Rust's concurrent tests.
    pub fn with_retention_root(outputs: Option<&Path>) -> Self {
        let prefix = current_test_prefix();
        let directory = TempDir::with_prefix(&prefix).expect("create case workspace");
        let path = directory.path().to_path_buf();
        let destination = outputs.map(|outputs| {
            outputs.join(format!(
                "{prefix}{}-{}",
                std::process::id(),
                CASE_COUNTER.fetch_add(1, Ordering::Relaxed),
            ))
        });
        let metadata = CaseMetadata {
            original_root: path.clone(),
            test: prefix,
        };
        fs::write(
            path.join("case.json"),
            serde_json::to_vec(&metadata).expect("serialize case metadata"),
        )
        .expect("write case metadata");
        Self {
            root: Arc::new(CaseRoot {
                directory,
                destination,
                retained: AtomicBool::new(false),
            }),
            directory: path,
        }
    }

    pub fn path(&self) -> &Path {
        &self.directory
    }

    /// A fresh invocation output directory, removed only with the whole case.
    pub fn child(&self) -> Self {
        let directory = TempDir::new_in(self.root.directory.path())
            .expect("create case invocation directory")
            .keep();
        Self {
            root: Arc::clone(&self.root),
            directory,
        }
    }

    fn retain(&self) -> io::Result<()> {
        let Some(destination) = &self.root.destination else {
            return Ok(());
        };
        if self.root.retained.swap(true, Ordering::Relaxed) {
            return Ok(());
        }
        copy_tree(self.root.directory.path(), destination)?;
        let _ = writeln!(
            io::stderr(),
            "retained failed case: {}",
            destination.display()
        );
        Ok(())
    }
}

impl Drop for CaseWorkspace {
    fn drop(&mut self) {
        if std::thread::panicking()
            && let Err(error) = self.retain()
        {
            // A second panic during unwinding aborts the process and hides the
            // assertion. Retention is diagnostic and must remain best effort.
            let _ = writeln!(io::stderr(), "could not retain failed case: {error}");
        }
    }
}

pub(super) fn copy_tree(source: &Path, destination: &Path) -> io::Result<()> {
    fs::create_dir_all(destination)?;
    for entry in fs::read_dir(source)? {
        let entry = entry?;
        let path = entry.path();
        let target = destination.join(entry.file_name());
        if entry.file_type()?.is_symlink() {
            std::os::unix::fs::symlink(fs::read_link(&path)?, &target)?;
        } else if path.is_dir() {
            copy_tree(&path, &target)?;
        } else {
            fs::copy(&path, &target)?;
        }
    }
    Ok(())
}
