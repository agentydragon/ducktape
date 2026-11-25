//! Opt-in full-context inputs for report-only effect relocation assessment on
//! an in-memory graph-backed edit candidate.
//!
//! These arguments deliberately remain separate from [`GraphEditArgs`]: the
//! graph gate is still authoritative, while an assessment requires a verified
//! staged artifact and the full tree transform context.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use transform_command::{SpecSourceArgs, TransformContextArgs, TransformContextAuxArgs};

use anyhow::{Context, Result, ensure};
use graph_provenance::VerifiedGraphSourceContext;
use pipeline::{AnalysisArtifactInput, TransformRequest};

use crate::binding::ModuleDocs;
use crate::edit_gate::GraphEditArgs;

#[derive(Debug, Clone, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum EffectAssessmentUnavailable {
    AnalysisNotSupplied,
}

/// Shared opt-in CLI flags used by graph-backed edits that can assess a full
/// in-memory candidate through the canonical lowering path.
#[derive(Debug, clap::Args)]
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
#[derive(Debug, clap::Args)]
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
    transform: TransformRequest,
    analysis_path: PathBuf,
    graph_source: &'a VerifiedGraphSourceContext,
    analysis_engine_sha256: &'static str,
}

impl CandidateEffectAssessmentArgs {
    /// Resolve opt-in assessment inputs and enforce the CLI trust boundary.
    /// With no `--analysis`, all transform-context flags must remain absent,
    /// preserving the ordinary graph-only edit path.
    pub fn resolve<'a>(
        self,
        edit: &GraphEditArgs,
        source_context: Option<&'a VerifiedGraphSourceContext>,
        analysis_engine_sha256: &'static str,
    ) -> Result<Option<ResolvedCandidateEffectAssessment<'a>>> {
        self.resolve_with_write_policy(edit, source_context, false, analysis_engine_sha256)
    }

    /// Callers use this only when they assess final candidate documents and
    /// enforce canonical placement before any write.
    pub fn resolve_for_verified_write<'a>(
        self,
        edit: &GraphEditArgs,
        source_context: Option<&'a VerifiedGraphSourceContext>,
        analysis_engine_sha256: &'static str,
    ) -> Result<Option<ResolvedCandidateEffectAssessment<'a>>> {
        self.resolve_with_write_policy(edit, source_context, true, analysis_engine_sha256)
    }

    fn resolve_with_write_policy<'a>(
        self,
        edit: &GraphEditArgs,
        source_context: Option<&'a VerifiedGraphSourceContext>,
        allow_write: bool,
        analysis_engine_sha256: &'static str,
    ) -> Result<Option<ResolvedCandidateEffectAssessment<'a>>> {
        let Some(analysis_path) = self.analysis else {
            ensure!(
                self.transform.source.spec.is_none()
                    && self.transform.source.tree_config.is_none()
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
            self.transform.source.tree_config.is_some() && self.transform.source.spec.is_none(),
            "candidate effect assessment currently requires the full --tree-config transform context"
        );
        ResolvedCandidateEffectAssessment::new(
            self.transform,
            analysis_path,
            graph_source,
            analysis_engine_sha256,
        )
        .map(Some)
    }
}

impl CandidateEffectAssessmentExistingTreeArgs {
    pub fn resolve_for_verified_write<'a>(
        self,
        modules_root: &Path,
        source_context: Option<&'a VerifiedGraphSourceContext>,
        tree_config: Option<&Path>,
        analysis_engine_sha256: &'static str,
    ) -> Result<Option<ResolvedCandidateEffectAssessment<'a>>> {
        let Some(analysis) = self.analysis else {
            ensure!(
                !self.auxiliary.has_any_inputs(),
                "transform context arguments require --analysis for candidate effect assessment"
            );
            return Ok(None);
        };
        let tree_config = tree_config
            .context("candidate effect assessment requires the command's --tree-config")?;
        let graph_source = source_context
            .context("candidate effect assessment requires a verified graph source context")?;
        ResolvedCandidateEffectAssessment::new(
            TransformContextArgs {
                source: SpecSourceArgs {
                    spec: None,
                    tree_config: Some(tree_config.to_path_buf()),
                    tree_modules: Some(modules_root.to_path_buf()),
                    tree: self.auxiliary.tree,
                },
                package_sources: self.auxiliary.package_sources,
            },
            analysis,
            graph_source,
            analysis_engine_sha256,
        )
        .map(Some)
    }
}

impl<'a> ResolvedCandidateEffectAssessment<'a> {
    fn new(
        transform: TransformContextArgs,
        analysis_path: PathBuf,
        graph_source: &'a VerifiedGraphSourceContext,
        analysis_engine_sha256: &'static str,
    ) -> Result<Self> {
        Ok(Self {
            transform: transform.resolve()?,
            analysis_path,
            graph_source,
            analysis_engine_sha256,
        })
    }

    /// Assess this complete typed module candidate using the exact verified
    /// graph input and canonical staged analysis pipeline.
    pub fn assess(
        &self,
        stderr: &mut (dyn std::io::Write + Send),
        edit_modules_root: &Path,
        docs: &ModuleDocs,
        deleted: &BTreeSet<String>,
        import_order: &[spec::ModulePath],
    ) -> Result<gate::CandidateRelocationAssessmentReport> {
        let candidate_modules = docs
            .iter()
            .filter(|(path, _)| !deleted.contains(*path))
            .map(|(path, (_, doc))| (path.clone(), doc.clone()))
            .collect();
        command_diagnostics::run(stderr, |diagnostics| {
            pipeline::assess_candidate_effect_relocation(
                &self.transform,
                AnalysisArtifactInput {
                    path: &self.analysis_path,
                    analysis_engine_sha256: self.analysis_engine_sha256,
                },
                self.graph_source,
                edit_modules_root,
                &candidate_modules,
                import_order,
                diagnostics,
            )
        })
    }
}
