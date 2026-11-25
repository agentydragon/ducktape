//! Command adapter for the standalone analyzer and `debundle analyze`.

use anyhow::Result;
use clap::Args as ClapArgs;
use package_args::{PackageRootArgs, collect_package_roots};
use staged_analysis::{AnalyzeRequest, analyze_from_projection};
use std::path::PathBuf;

/// Command-line arguments for `debundle analyze`.
#[derive(ClapArgs, Debug, Clone)]
pub struct AnalyzeArgs {
    /// Analysis projection written by `debundle spec project-analysis`; the
    /// only spec input.
    #[arg(long)]
    pub projection: PathBuf,
    /// Root the projection's source paths are relative to.
    #[arg(long = "tree-source-root", env = "DEBUNDLE_TREE_SOURCE_ROOT")]
    pub tree_source_root: PathBuf,
    #[command(flatten)]
    pub package_sources: PackageRootArgs,
    /// Where to write the analysis artifact.
    #[arg(long)]
    pub out: PathBuf,
    /// Write OpenTelemetry metrics as JSONL to this file.
    #[arg(long, value_name = "PATH")]
    pub metrics_file: Option<PathBuf>,
}

impl AnalyzeArgs {
    pub fn resolve(self) -> Result<AnalyzeRequest> {
        Ok(AnalyzeRequest {
            projection: self.projection,
            tree_source_root: self.tree_source_root,
            package_roots: collect_package_roots(self.package_sources.package_roots)?
                .into_iter()
                .collect(),
            packages_root: self.package_sources.packages_root,
            out: self.out,
        })
    }

    pub fn run(
        mut self,
        stderr: &mut (dyn std::io::Write + Send),
        analysis_engine_sha256: &str,
    ) -> Result<()> {
        let metrics_file = self.metrics_file.take();
        command_diagnostics::run_with_metrics(stderr, metrics_file.as_deref(), |diagnostics| {
            analyze_from_projection(self.resolve()?, analysis_engine_sha256, diagnostics)
        })
    }
}
