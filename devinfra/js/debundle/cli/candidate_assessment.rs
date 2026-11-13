//! Opt-in full-context inputs for report-only effect relocation assessment on
//! an in-memory graph-backed edit candidate.
//!
//! These arguments deliberately remain separate from [`GraphEditArgs`]: the
//! graph gate is still authoritative, while an assessment requires a verified
//! staged artifact and the full tree transform context.

use std::collections::BTreeMap;
use std::path::PathBuf;
use transform_command::{TransformContextArgs, TransformContextAuxArgs};

use anyhow::{Context, Result, ensure};
use graph_provenance::VerifiedGraphSourceContext;
use pipeline::{AnalysisArtifactInput, TransformRequest};
use spec::LogicalModule;

use crate::edit_gate::GraphEditArgs;

#[derive(Debug, Clone, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum EffectAssessmentUnavailable {
    AnalysisNotSupplied,
}

/// Shared opt-in CLI flags used by graph-backed edits that can assess a full
/// in-memory candidate through the canonical lowering path.
#[derive(Debug, clap::Args, Clone, Default)]
pub struct CandidateEffectAssessmentArgs {
    /// Full tree transform inputs used only with `--analysis`.
    #[command(flatten)]
    pub transform: TransformContextArgs,
    /// Verified staged analysis artifact for the candidate effect assessment.
    #[arg(long)]
    pub analysis: Option<PathBuf>,
}

/// Candidate assessment options for a command that already owns its tree
/// config and module root arguments. The auxiliary transform inputs are the
/// same shared group used by [`TransformContextArgs`]. The command registration
/// decides whether source/output environment defaults are available.
#[derive(Debug, clap::Args, Clone, Default)]
pub struct CandidateEffectAssessmentExistingTreeArgs {
    /// Verified staged analysis artifact for the candidate effect assessment.
    #[arg(long)]
    pub analysis: Option<PathBuf>,
    /// Remaining tree source, output, and package inputs for assessment.
    #[command(flatten)]
    pub auxiliary: TransformContextAuxArgs,
}

/// Inputs resolved once for an explicit effect assessment request.
pub struct ResolvedCandidateEffectAssessment<'a> {
    pub transform: TransformRequest,
    pub analysis_path: PathBuf,
    pub graph_source: &'a VerifiedGraphSourceContext,
}

impl CandidateEffectAssessmentArgs {
    /// Resolve opt-in assessment inputs and enforce the CLI trust boundary.
    /// With no `--analysis`, all transform-context flags must remain absent,
    /// preserving the ordinary graph-only edit path.
    pub fn resolve<'a>(
        &self,
        edit: &GraphEditArgs,
        source_context: Option<&'a VerifiedGraphSourceContext>,
    ) -> Result<Option<ResolvedCandidateEffectAssessment<'a>>> {
        self.resolve_with_write_policy(edit, source_context, false)
    }

    /// Callers use this only when they assess final candidate documents and
    /// enforce canonical placement before any write.
    pub fn resolve_for_verified_write<'a>(
        &self,
        edit: &GraphEditArgs,
        source_context: Option<&'a VerifiedGraphSourceContext>,
    ) -> Result<Option<ResolvedCandidateEffectAssessment<'a>>> {
        self.resolve_with_write_policy(edit, source_context, true)
    }

    fn resolve_with_write_policy<'a>(
        &self,
        edit: &GraphEditArgs,
        source_context: Option<&'a VerifiedGraphSourceContext>,
        allow_write: bool,
    ) -> Result<Option<ResolvedCandidateEffectAssessment<'a>>> {
        let Some(analysis_path) = &self.analysis else {
            ensure!(
                self.transform.spec.is_none()
                    && self.transform.tree_config.is_none()
                    && !self.transform.has_explicit_auxiliary_inputs(),
                "transform context arguments require --analysis for candidate effect assessment"
            );
            return Ok(None);
        };

        ensure!(
            edit.dry_run || allow_write,
            "candidate effect assessment requires --dry-run"
        );
        ensure!(
            !edit.no_verify && edit.dependency_graph_path.is_some(),
            "candidate effect assessment requires graph validation"
        );
        let graph_source = source_context
            .context("candidate effect assessment requires a verified graph source context")?;
        ensure!(
            self.transform.tree_config.is_some() && self.transform.spec.is_none(),
            "candidate effect assessment currently requires the full --tree-config transform context"
        );
        Ok(Some(ResolvedCandidateEffectAssessment {
            transform: self.transform.clone().resolve()?,
            analysis_path: analysis_path.clone(),
            graph_source,
        }))
    }
}

impl CandidateEffectAssessmentExistingTreeArgs {
    /// Resolve assessment inputs using the command's existing tree
    /// config and module-root flags, so each option has one CLI owner.
    pub fn resolve<'a>(
        &self,
        edit: &GraphEditArgs,
        source_context: Option<&'a VerifiedGraphSourceContext>,
        tree_config: Option<&std::path::Path>,
    ) -> Result<Option<ResolvedCandidateEffectAssessment<'a>>> {
        self.resolve_with_write_policy(edit, source_context, tree_config, false)
    }

    pub fn resolve_for_verified_write<'a>(
        &self,
        edit: &GraphEditArgs,
        source_context: Option<&'a VerifiedGraphSourceContext>,
        tree_config: Option<&std::path::Path>,
    ) -> Result<Option<ResolvedCandidateEffectAssessment<'a>>> {
        self.resolve_with_write_policy(edit, source_context, tree_config, true)
    }

    fn resolve_with_write_policy<'a>(
        &self,
        edit: &GraphEditArgs,
        source_context: Option<&'a VerifiedGraphSourceContext>,
        tree_config: Option<&std::path::Path>,
        allow_write: bool,
    ) -> Result<Option<ResolvedCandidateEffectAssessment<'a>>> {
        let Some(analysis) = &self.analysis else {
            ensure!(
                !self.auxiliary.has_any_inputs(),
                "transform context arguments require --analysis for candidate effect assessment"
            );
            return Ok(None);
        };
        let tree_config = tree_config
            .context("candidate effect assessment requires the command's --tree-config")?;
        let args = CandidateEffectAssessmentArgs {
            transform: TransformContextArgs {
                spec: None,
                tree_config: Some(tree_config.to_path_buf()),
                tree_modules: Some(edit.modules_root.clone()),
                auxiliary: self.auxiliary.clone(),
            },
            analysis: Some(analysis.clone()),
        };
        args.resolve_with_write_policy(edit, source_context, allow_write)
    }
}

impl ResolvedCandidateEffectAssessment<'_> {
    /// Assess this complete typed module candidate using the exact verified
    /// graph input and canonical staged analysis pipeline.
    pub fn assess(
        &self,
        stderr: &mut dyn std::io::Write,
        edit_modules_root: &std::path::Path,
        candidate_modules: &BTreeMap<String, LogicalModule>,
        analysis_engine_sha256: &'static str,
    ) -> Result<gate::CandidateRelocationAssessmentReport> {
        command_diagnostics::run(stderr, |diagnostics| {
            pipeline::assess_candidate_effect_relocation(
                &self.transform,
                AnalysisArtifactInput {
                    path: &self.analysis_path,
                    analysis_engine_sha256,
                },
                self.graph_source,
                edit_modules_root,
                candidate_modules,
                diagnostics,
            )
        })
        .context("candidate effect assessment failed")
    }
}
