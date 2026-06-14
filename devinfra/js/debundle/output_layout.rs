use std::path::{Path, PathBuf};

pub const APP_DIR: &str = "app";
pub const REPORTS_DIR: &str = "reports";
pub const TREE_DIR: &str = "tree";

pub const RUNTIME_REPORT: &str = "runtime.json";
pub const OUTPUT_REPORT: &str = "output.json";
pub const CHUNKS_REPORT: &str = "chunks.json";
pub const SOURCE_ASSETS_REPORT: &str = "source_assets.json";
pub const PROVENANCE_REPORT: &str = "provenance.json";
pub const RENAME_QUEUE_REPORT: &str = "rename_queue.json";
pub const VENDOR_SWAPS_REPORT: &str = "vendor_swaps.json";

pub const CHUNK_REPORT: &str = "chunk.json";
pub const MODULES_REPORT: &str = "modules.json";
pub const OWNER_GRAPH_REPORT: &str = "owner_graph.json";
pub const CYCLES_REPORT: &str = "cycles.json";
pub const ATOMIC_UNIT_CONFLICTS_REPORT: &str = "atomic_unit_conflicts.json";
pub const SELECTOR_DIAGNOSTICS_REPORT: &str = "selector_diagnostics.json";
pub const INDEX_REPORT: &str = "index.json";

#[derive(Debug, Clone)]
pub struct DebundleOutputLayout {
    root: PathBuf,
}

impl DebundleOutputLayout {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn app_root(&self) -> PathBuf {
        self.root.join(APP_DIR)
    }

    pub fn reports_root(&self) -> PathBuf {
        self.root.join(REPORTS_DIR)
    }

    pub fn tree_root(&self) -> PathBuf {
        self.reports_root().join(TREE_DIR)
    }

    pub fn runtime_report(&self) -> PathBuf {
        self.reports_root().join(RUNTIME_REPORT)
    }

    pub fn output_report(&self) -> PathBuf {
        self.reports_root().join(OUTPUT_REPORT)
    }

    pub fn chunks_report(&self) -> PathBuf {
        self.reports_root().join(CHUNKS_REPORT)
    }

    pub fn source_assets_report(&self) -> PathBuf {
        self.reports_root().join(SOURCE_ASSETS_REPORT)
    }

    pub fn provenance_report(&self) -> PathBuf {
        self.reports_root().join(PROVENANCE_REPORT)
    }

    pub fn rename_queue_report(&self) -> PathBuf {
        self.reports_root().join(RENAME_QUEUE_REPORT)
    }

    pub fn vendor_swaps_report(&self) -> PathBuf {
        self.reports_root().join(VENDOR_SWAPS_REPORT)
    }
}

pub fn report_path_for_file(tree_root: &Path, app_relative_path: &str) -> PathBuf {
    tree_root
        .join(app_relative_path.split('/').collect::<PathBuf>())
        .with_file_name(format!(
            "{}.json",
            Path::new(app_relative_path)
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or(app_relative_path)
        ))
}

pub fn report_path_for_directory(tree_root: &Path, directory: &str) -> PathBuf {
    if directory.is_empty() {
        tree_root.join(INDEX_REPORT)
    } else {
        tree_root
            .join(directory.split('/').collect::<PathBuf>())
            .join(INDEX_REPORT)
    }
}
