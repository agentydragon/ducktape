use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Result, bail};
use relative_path::RelativePath;

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
/// Written at the output root by `debundle run --emit-on-rejection` when the
/// realizability gate rejected the spec: the tree beside it is not runnable.
pub const GATE_REJECTED_MARKER: &str = "GATE_REJECTED";

pub const CHUNK_REPORT: &str = "chunk.json";
pub const MODULES_REPORT: &str = "modules.json";
pub const GRAPH_SUMMARY_REPORT: &str = "graph_summary.json";
pub const DEPENDENCY_GRAPH_REPORT: &str = "dependency_graph.json.gz";
/// Program-wide graph sections, written once at the report root and
/// referenced by every chunk's `dependency_graph.json.gz`.
pub const PROGRAM_GRAPH_REPORT: &str = "program_graph.json.gz";
pub const CYCLES_REPORT: &str = "cycles.json";
pub const ATOMIC_GROUP_CONFLICTS_REPORT: &str = "atomic_group_conflicts.json";
pub const RESIDUAL_ATOMIC_GROUPS_REPORT: &str = "residual_atomic_groups.json";
pub const RESIDUAL_EXTRACTIONS_REPORT: &str = "residual_extractions.json";
pub const SELECTOR_DIAGNOSTICS_REPORT: &str = "selector_diagnostics.json";
/// Written beside `selector_diagnostics.json` when the graph build diagnoses
/// `commuting_statements` entries: the contradiction that failed the chunk,
/// or one warning per unused entry.
pub const COMMUTING_STATEMENTS_DIAGNOSTICS_REPORT: &str = "commuting_statements_diagnostics.json";
/// Written per chunk whenever a report directory is: the chunk's
/// `reports::spec_lint::LintFinding`s, read back by `spec lint`.
pub const SPEC_LINT_REPORT: &str = "spec_lint.json";
pub const EXPORT_REENTRY_REPORT: &str = "export_reentry.json";
pub const EXPORT_REENTRY_FULL_REPORT: &str = "export_reentry.full.json";
/// Translation validation beside the gate's verdict; report only.
pub const EFFECT_CONFLICTS_JSONL: &str = "effect_conflicts.jsonl";
pub const EFFECT_TRANSLATION_VALIDATION_REPORT: &str = "effect_translation_validation.json";
pub const CANDIDATE_RELOCATION_ASSESSMENT_REPORT: &str = "candidate_relocation_assessment.json";
/// Program-wide, written once at the report root of an accepted run.
pub const GLOBAL_RESOLUTION_REPORT: &str = "global_resolution.json";
pub const TRANSLATION_VALIDATION_REPORT: &str = "translation_validation.json";
pub const TRANSLATION_VALIDATION_FULL_REPORT: &str = "translation_validation.full.jsonl";
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

    /// Create `app/` and `reports/`; bail if `root` exists and is not a directory.
    pub fn prepare(&self) -> Result<()> {
        if self.root.exists() && !self.root.is_dir() {
            bail!(
                "Output path exists and is not a directory: {}",
                self.root.display()
            );
        }
        fs::create_dir_all(self.app_root())?;
        fs::create_dir_all(self.reports_root())?;
        Ok(())
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
        .join(RelativePath::new(app_relative_path).to_path(""))
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
            .join(RelativePath::new(directory).to_path(""))
            .join(INDEX_REPORT)
    }
}
