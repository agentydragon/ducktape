//! Concrete reconstructed program and the capability required by output writers.
//!
//! Planning has already chosen file placement, names, directives, evaluation
//! order, and vendor replacements. The syntax in this plan includes those
//! decisions. Final validation inspects that exact syntax; accepted plans expose
//! no mutation path, so emission cannot change the program after validation.
//! A gate-rejected program is frozen as a [`RejectedPlan`] instead, which only
//! the marked reading-only tree writer consumes.

use source_identity::ChunkId;
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use anyhow::{Context, Result, bail, ensure};
use artifact::EmissionFiles;
use artifact::reports::write_json;
use gate::ApplicationRelocationAssessmentReport;
use gate::{ChunkGateRejection, UnmatchedSpecClaim};
use lowering::FinalizedCandidateEmissionDraft;
use validate_emitted_exports::validate_emitted_exports;
use vendor::{
    VendorResolutionPlan, VendorSwapsReport, validate_partial_swap_consumers,
    write_planned_vendor_outputs,
};

#[path = "artifacts/browser_harness_plan.rs"]
pub mod browser_harness_plan;

use browser_harness_plan::BrowserHarnessPlan;

/// The manifest and destination frozen alongside the program they describe.
pub struct VendorManifestOutput {
    path: PathBuf,
    report: VendorSwapsReport,
}

/// Exact final files whose canonical source and candidate obligations passed.
/// The only constructor is `assess_final_candidate_emission`, which consumes
/// the private finalized lowering product and runs the joint checker.
pub struct AcceptedCandidateEmission {
    files: EmissionFiles,
    vendor_plan: Arc<VendorResolutionPlan>,
    assessment: ApplicationRelocationAssessmentReport,
}

impl AcceptedCandidateEmission {
    pub fn files(&self) -> &EmissionFiles {
        &self.files
    }

    pub fn assessment(&self) -> &ApplicationRelocationAssessmentReport {
        &self.assessment
    }
}

/// Exact final files that failed or could not complete the joint check. This
/// capability is inspection-only and is consumed by `RejectedPlan`.
pub struct RefusedCandidateEmission {
    files: EmissionFiles,
    assessment: ApplicationRelocationAssessmentReport,
}

impl RefusedCandidateEmission {
    pub fn files(&self) -> &EmissionFiles {
        &self.files
    }

    pub fn assessment(&self) -> &ApplicationRelocationAssessmentReport {
        &self.assessment
    }
}

pub enum CandidateEmissionOutcome {
    Accepted(AcceptedCandidateEmission),
    Refused(RefusedCandidateEmission),
}

impl CandidateEmissionOutcome {
    pub fn files(&self) -> &EmissionFiles {
        match self {
            Self::Accepted(candidate) => candidate.files(),
            Self::Refused(candidate) => candidate.files(),
        }
    }

    pub fn assessment(&self) -> &ApplicationRelocationAssessmentReport {
        match self {
            Self::Accepted(candidate) => candidate.assessment(),
            Self::Refused(candidate) => candidate.assessment(),
        }
    }

    pub fn into_accepted(self) -> Option<AcceptedCandidateEmission> {
        match self {
            Self::Accepted(candidate) => Some(candidate),
            Self::Refused(_) => None,
        }
    }

    pub fn into_refused(self) -> Option<RefusedCandidateEmission> {
        match self {
            Self::Accepted(_) => None,
            Self::Refused(candidate) => Some(candidate),
        }
    }
}

/// The canonical reconstruction boundary consumes a finalized, file-owning
/// lowerer product. Public reports and raw `EmissionFiles` cannot mint either
/// candidate capability.
pub fn assess_final_candidate_emission(
    draft: FinalizedCandidateEmissionDraft,
) -> CandidateEmissionOutcome {
    let (files, assessment, source_inputs, source_correspondence, global_use_elisions, vendor_plan) =
        draft.into_final_check_parts();
    let assessment = gate::assess_final_candidate_emission(
        assessment,
        &source_inputs,
        &source_correspondence,
        files.files(),
        &global_use_elisions,
    );
    let globally_accepted = assessment.status
        == gate::CandidateRelocationStatus::AcceptedWithinCheckedScope
        && assessment.global_resolution.as_ref().is_some_and(|global| {
            global.status == gate::GlobalResolutionStatus::Preserved
                && global.source_correspondence_status
                    == Some(gate::GlobalResolutionStatus::Preserved)
        })
        && assessment.final_output_is_sealed();
    if globally_accepted {
        CandidateEmissionOutcome::Accepted(AcceptedCandidateEmission {
            files,
            vendor_plan,
            assessment,
        })
    } else {
        CandidateEmissionOutcome::Refused(RefusedCandidateEmission { files, assessment })
    }
}

impl VendorManifestOutput {
    /// No manifest is emitted without a destination or any replacements.
    pub fn prepare(path: Option<&Path>, report: VendorSwapsReport) -> Option<Self> {
        if report.is_empty() {
            return None;
        }
        Some(Self {
            path: path?.to_path_buf(),
            report,
        })
    }
}

/// Ready-to-validate output syntax and replacement decisions for one program.
/// Diagnostic reports are separate from the accepted output tree.
pub struct ReconstructionPlan {
    candidate: AcceptedCandidateEmission,
    vendor_manifest: Option<VendorManifestOutput>,
    unresolved_claims: Vec<UnmatchedSpecClaim>,
    browser_harness: Option<BrowserHarnessPlan>,
}

impl ReconstructionPlan {
    pub fn new(
        candidate: AcceptedCandidateEmission,
        unresolved_claims: Vec<UnmatchedSpecClaim>,
        browser_options: Option<&spec::EmitBrowserHarnessConfig>,
        vendor_manifest: Option<VendorManifestOutput>,
    ) -> Result<Self> {
        let vendor = &candidate.vendor_plan;
        let browser_harness = browser_options
            .map(|options| {
                BrowserHarnessPlan::prepare(
                    candidate.files().files(),
                    &vendor.full_swap_chunk_ids(),
                    &vendor
                        .full_swap_resolutions()
                        .values()
                        .filter_map(|resolution| {
                            candidate
                                .files()
                                .files()
                                .chunk_table
                                .get(&resolution.chunk_id)
                                .map(|id| (id, resolution.package.clone()))
                        })
                        .collect(),
                    options,
                )
            })
            .transpose()?;
        Ok(Self {
            candidate,
            vendor_manifest,
            unresolved_claims,
            browser_harness,
        })
    }

    /// Validate the exact reconstructed syntax after all body transformations.
    /// Keep both post-reconstruction safeguards: synthesized directives are not
    /// fully covered by the input-space replacement checks.
    pub fn validate(self) -> Result<ValidatedPlan> {
        validate_partial_swap_consumers(self.candidate.files(), &self.candidate.vendor_plan)?;
        let excluded_chunk_ids = self.candidate.vendor_plan.full_swap_chunk_ids();
        validate_emitted_exports(self.candidate.files(), &excluded_chunk_ids)?;
        reject_unresolved_claims(&self.unresolved_claims)?;
        Ok(ValidatedPlan {
            candidate: self.candidate,
            vendor_manifest: self.vendor_manifest,
            excluded_chunk_ids,
            browser_harness: self.browser_harness,
        })
    }
}

/// An immutable concrete plan accepted by the shared final validator.
/// Only `ReconstructionPlan::validate` can construct this capability.
pub struct ValidatedPlan {
    candidate: AcceptedCandidateEmission,
    vendor_manifest: Option<VendorManifestOutput>,
    excluded_chunk_ids: BTreeSet<ChunkId>,
    browser_harness: Option<BrowserHarnessPlan>,
}

impl ValidatedPlan {
    pub fn files(&self) -> &EmissionFiles {
        self.candidate.files()
    }

    pub fn candidate_assessment(&self) -> &gate::ApplicationRelocationAssessmentReport {
        self.candidate.assessment()
    }

    pub fn excluded_chunk_ids(&self) -> &BTreeSet<ChunkId> {
        &self.excluded_chunk_ids
    }

    pub fn browser_harness(&self) -> Option<&BrowserHarnessPlan> {
        self.browser_harness.as_ref()
    }

    /// Write the replacement assets and manifest belonging to this accepted program.
    pub fn write_vendor_outputs(&self) -> Result<()> {
        write_planned_vendor_outputs(&self.candidate.vendor_plan)?;
        if let Some(manifest) = &self.vendor_manifest {
            write_json(&manifest.path, &manifest.report)?;
        }
        Ok(())
    }
}

/// A program the realizability gate rejected, frozen for the reading-only tree
/// `debundle run --emit-on-rejection` writes. It never reaches final program
/// validation and exposes no browser harness or vendor replacement outputs:
/// those describe a runnable bundle. The tree writer marks the output.
pub struct RejectedPlan {
    candidate: RefusedCandidateEmission,
    excluded_chunk_ids: BTreeSet<ChunkId>,
    rejections: Vec<ChunkGateRejection>,
}

impl RejectedPlan {
    /// Fails, naming the gate's diagnostic too, on unresolved spec claims: a
    /// reading-only tree carries the gate's rejection and no other failure.
    pub fn new(
        candidate: RefusedCandidateEmission,
        vendor: &VendorResolutionPlan,
        rejections: Vec<ChunkGateRejection>,
        unresolved_claims: &[UnmatchedSpecClaim],
    ) -> Result<Self> {
        ensure!(
            !rejections.is_empty(),
            "a rejected plan needs at least one gate rejection"
        );
        let plan = Self {
            candidate,
            excluded_chunk_ids: vendor.full_swap_chunk_ids(),
            rejections,
        };
        reject_unresolved_claims(unresolved_claims).with_context(|| plan.diagnostic())?;
        Ok(plan)
    }

    pub fn files(&self) -> &EmissionFiles {
        self.candidate.files()
    }

    pub fn excluded_chunk_ids(&self) -> &BTreeSet<ChunkId> {
        &self.excluded_chunk_ids
    }

    pub fn rejections(&self) -> &[ChunkGateRejection] {
        &self.rejections
    }

    /// Every chunk's rendered rejection, in chunk order: the error the run
    /// fails with after writing the marked tree.
    pub fn diagnostic(&self) -> String {
        self.rejections
            .iter()
            .map(|rejection| rejection.diagnostic.as_str())
            .collect::<Vec<_>>()
            .join("\n")
    }
}

/// What the tree writer may materialize. A rejected plan is written only with
/// the gate-rejected marker and never with the browser harness or vendor assets.
#[derive(Clone, Copy)]
pub enum EmittablePlan<'a> {
    Accepted(&'a ValidatedPlan),
    Rejected(&'a RejectedPlan),
}

impl EmittablePlan<'_> {
    pub fn files(&self) -> &EmissionFiles {
        match self {
            Self::Accepted(plan) => plan.files(),
            Self::Rejected(plan) => plan.files(),
        }
    }

    pub fn excluded_chunk_ids(&self) -> &BTreeSet<ChunkId> {
        match self {
            Self::Accepted(plan) => plan.excluded_chunk_ids(),
            Self::Rejected(plan) => plan.excluded_chunk_ids(),
        }
    }
}

fn reject_unresolved_claims(claims: &[UnmatchedSpecClaim]) -> Result<()> {
    if claims.is_empty() {
        return Ok(());
    }
    let mut lines = String::new();
    for claim in claims {
        lines.push_str(&format!(
            "  - chunk {chunk_id}: module {module_path} claims binding `{binding}` (export `{export}`); \
             no top-level declaration with that name exists in the chunk.\n",
            chunk_id = claim.chunk_id,
            module_path = claim.module_path,
            binding = claim.binding_name,
            export = claim.export_name,
        ));
    }
    bail!(
        "Transform spec referenced {n} top-level binding name(s) that the source chunk \
         does not declare. Unresolved spec claims fall into the residual sweep and \
         leave the named destination module short an export. Fix the spec (correct \
         the binding name, drop the member, or rename via `chunk_renames`):\n{lines}",
        n = claims.len(),
    );
}
