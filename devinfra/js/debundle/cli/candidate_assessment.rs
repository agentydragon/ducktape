//! Full-context inputs for candidate relocation assessment on
//! an in-memory graph-backed edit candidate.
//!
//! These arguments deliberately remain separate from [`GraphEditArgs`]: the
//! verified ownership writes require an assessment from a staged artifact
//! and the full tree transform context.

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

/// Rejection of a verified write, carrying the exact canonical report that
/// caused it. Both text errors and JSON use this same decision.
#[derive(Debug)]
pub struct CandidateAssessmentRejection {
    pub report: gate::ApplicationRelocationAssessmentReport,
    reason: anyhow::Error,
}

impl std::fmt::Display for CandidateAssessmentRejection {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.reason)
    }
}

impl std::error::Error for CandidateAssessmentRejection {}

pub fn ensure_candidate_write(
    assessment: Option<&gate::ApplicationRelocationAssessmentReport>,
    dry_run: bool,
    no_verify: bool,
) -> Result<()> {
    if dry_run || no_verify {
        return Ok(());
    }
    let assessment = assessment.context(
        "validated ownership write needs_full_assessment: supply --analysis and complete tree transform context, or use --no-verify for an explicitly unvalidated write"
    )?;
    pipeline::ensure_candidate_emittable_for_write(assessment, false).map_err(|reason| {
        CandidateAssessmentRejection {
            report: assessment.clone(),
            reason,
        }
        .into()
    })
}

/// Shared CLI flags for assessing a full in-memory candidate through the
/// canonical lowering path.
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
    /// Callers use this only when they assess final candidate documents and
    /// enforce canonical placement before any write.
    pub fn resolve_for_verified_write<'a>(
        &self,
        edit: &GraphEditArgs,
        source_context: Option<&'a VerifiedGraphSourceContext>,
    ) -> Result<Option<ResolvedCandidateEffectAssessment<'a>>> {
        self.resolve_candidate(edit, source_context)
    }

    fn resolve_candidate<'a>(
        &self,
        edit: &GraphEditArgs,
        source_context: Option<&'a VerifiedGraphSourceContext>,
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
    pub fn resolve_for_verified_write<'a>(
        &self,
        edit: &GraphEditArgs,
        source_context: Option<&'a VerifiedGraphSourceContext>,
        tree_config: Option<&std::path::Path>,
    ) -> Result<Option<ResolvedCandidateEffectAssessment<'a>>> {
        self.resolve_candidate(edit, source_context, tree_config)
    }

    fn resolve_candidate<'a>(
        &self,
        edit: &GraphEditArgs,
        source_context: Option<&'a VerifiedGraphSourceContext>,
        tree_config: Option<&std::path::Path>,
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
        args.resolve_candidate(edit, source_context)
    }
}

impl ResolvedCandidateEffectAssessment<'_> {
    /// Assess this complete typed module candidate using the exact verified
    /// graph input and canonical staged analysis pipeline.
    pub fn assess(
        &self,
        edit_modules_root: &std::path::Path,
        candidate_modules: &BTreeMap<String, LogicalModule>,
        analysis_engine_sha256: &'static str,
    ) -> Result<gate::ApplicationRelocationAssessmentReport> {
        pipeline::assess_candidate_effect_relocation(
            &self.transform,
            AnalysisArtifactInput {
                path: &self.analysis_path,
                analysis_engine_sha256,
            },
            self.graph_source,
            edit_modules_root,
            candidate_modules,
        )
        .context("candidate effect assessment failed")
    }
}
