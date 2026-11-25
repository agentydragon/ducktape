//! Command syntax and environment policy for transform and projection requests.

use std::path::PathBuf;

use anyhow::{Result, bail};
use clap::Args as ClapArgs;
use pipeline::{RunOutput, project_analysis_and_apply};
pub use transform_inputs::{
    SpecSourceArgs, TransformContextArgs, TransformContextAuxArgs, is_inactive_transform_default,
    validate_transform_arg_matches,
};

/// Arguments for the standalone projection executable and
/// `debundle spec project-analysis`.
#[derive(Debug, ClapArgs)]
pub struct ProjectAnalysisArgs {
    #[command(flatten)]
    transform: TransformArgs,
    /// Write OpenTelemetry metrics as JSONL to this file.
    #[arg(long, value_name = "PATH")]
    metrics_file: Option<PathBuf>,
    /// Where to write the projection.
    #[arg(long)]
    out: PathBuf,
    /// Also write the canonical apply projection from the same parsed spec.
    #[arg(long)]
    apply_out: Option<PathBuf>,
    /// Root used to make paths in the apply projection relocatable.
    #[arg(long, default_value = ".")]
    projection_root: PathBuf,
}

impl ProjectAnalysisArgs {
    /// Resolve the shared transform arguments and write the canonical
    /// analysis projection.
    pub fn run(self, stderr: &mut (dyn std::io::Write + Send)) -> Result<()> {
        command_diagnostics::run_with_metrics(stderr, self.metrics_file.as_deref(), |diagnostics| {
            project_analysis_and_apply(
                &self.transform.context.resolve()?,
                &self.out,
                self.apply_out
                    .as_ref()
                    .map(|out| (self.projection_root.as_path(), out.as_path())),
                diagnostics,
            )
        })
    }
}

/// Project the inputs of a default apply action without parsing JavaScript.
#[derive(Debug, ClapArgs)]
pub struct ProjectApplyArgs {
    #[command(flatten)]
    context: TransformContextArgs,
    #[arg(long)]
    out: PathBuf,
    /// Root against which the projection stores relocatable filesystem paths.
    #[arg(long, default_value = ".")]
    projection_root: PathBuf,
}

impl ProjectApplyArgs {
    pub fn run(self) -> Result<()> {
        pipeline::project_apply(&self.context.resolve()?, &self.projection_root, &self.out)
    }
}

/// Run-only output and diagnostic flags layered over transform inputs.
#[derive(ClapArgs, Debug, Clone)]
pub struct TransformArgs {
    #[command(flatten)]
    pub context: TransformContextArgs,
    /// Run pipeline parse/facts/gate checks without writing emitted JS
    /// or accept-path reports. A gate rejection still writes its evidence
    /// under reports/tree/<chunk>/. Add --graphs for graph-backed gate queries.
    #[arg(long)]
    pub dry_run: bool,
    /// When the realizability gate rejects the spec, still write the
    /// logical-module tree for reading — marked unrunnable by a
    /// `GATE_REJECTED` file at the output root and a banner comment on each
    /// rejected chunk's entry file — without the browser harness or vendor
    /// assets. The gate reports are written as usual and the run still exits
    /// nonzero. Accepted specs are unaffected.
    #[arg(long = "emit-on-rejection", conflicts_with = "dry_run")]
    pub emit_on_rejection: bool,
    /// Stop at the first supported diagnostic instead of collecting all
    /// findings from the pass. Broad runs otherwise aggregate every
    /// supported diagnostic — currently unresolved source-match
    /// selectors and duplicate binding claims, with
    /// module/export/origin evidence — before failing.
    #[arg(long)]
    pub fail_fast: bool,
}

impl TransformArgs {
    /// `--dry-run` / `--emit-on-rejection` as one output mode; Clap already
    /// refuses both together.
    pub fn run_output(&self) -> Result<RunOutput> {
        match (self.dry_run, self.emit_on_rejection) {
            (false, false) => Ok(RunOutput::Accepted),
            (true, false) => Ok(RunOutput::DryRun),
            (false, true) => Ok(RunOutput::EmitOnRejection),
            (true, true) => bail!("--dry-run and --emit-on-rejection are mutually exclusive"),
        }
    }
}
