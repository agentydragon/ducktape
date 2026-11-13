//! Command adapter for the standalone analyzer and `debundle analyze`.

use anyhow::Result;
use clap::Args as ClapArgs;
use package_args::{collect_package_roots, parse_package_root_kv};
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
    /// Map a package name to its source directory: `<pkg>=<dir>`. May be repeated.
    #[arg(long = "package-root", value_parser = parse_package_root_kv)]
    pub package_roots: Vec<(String, PathBuf)>,
    /// Root directory containing per-package sources (alternative to repeated --package-root).
    #[arg(long)]
    pub packages_root: Option<PathBuf>,
    /// Where to write the analysis artifact.
    #[arg(long)]
    pub out: PathBuf,
}

impl AnalyzeArgs {
    pub fn resolve(self) -> Result<AnalyzeRequest> {
        Ok(AnalyzeRequest {
            projection: self.projection,
            tree_source_root: self.tree_source_root,
            package_roots: collect_package_roots(self.package_roots)?
                .into_iter()
                .collect(),
            packages_root: self.packages_root,
            out: self.out,
        })
    }

    pub fn run(self, stderr: &mut dyn std::io::Write, analysis_engine_sha256: &str) -> Result<()> {
        let diagnostics = analysis::diagnostics::Diagnostics::default();
        let result = analyze_from_projection(self.resolve()?, analysis_engine_sha256, &diagnostics);
        command_diagnostics::finish(result, command_diagnostics::report(stderr, &diagnostics))
    }
}
