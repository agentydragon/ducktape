//! Opt-in full-context inputs for report-only effect relocation assessment on
//! an in-memory graph-backed edit candidate.
//!
//! These arguments deliberately remain separate from [`GraphEditArgs`]: the
//! graph gate is still authoritative, while an assessment requires a verified
//! staged artifact and the full tree transform context.

use std::collections::BTreeMap;
use std::path::PathBuf;

use anyhow::{Context, Result, ensure};
use graph_provenance::VerifiedGraphSourceContext;
use pipeline::{AnalysisArtifactInput, TransformCli, TransformContextArgs};
use spec::LogicalModule;

use crate::edit_gate::GraphEditArgs;

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

/// Inputs resolved once for an explicit effect assessment request.
pub struct ResolvedCandidateEffectAssessment<'a> {
    pub transform: TransformCli,
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
            edit.dry_run,
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

impl ResolvedCandidateEffectAssessment<'_> {
    /// Assess this complete typed module candidate using the exact verified
    /// graph input and canonical staged analysis pipeline.
    pub fn assess(
        &self,
        edit_modules_root: &std::path::Path,
        candidate_modules: &BTreeMap<String, LogicalModule>,
    ) -> Result<gate::EffectRelocationAssessment> {
        pipeline::assess_candidate_effect_relocation(
            &self.transform,
            AnalysisArtifactInput {
                path: &self.analysis_path,
                analysis_engine_sha256: analysis_identity::ANALYSIS_ENGINE_SHA256,
            },
            self.graph_source,
            edit_modules_root,
            candidate_modules,
        )
        .context("candidate effect assessment failed")
    }
}
