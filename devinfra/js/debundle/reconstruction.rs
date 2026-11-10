//! Concrete reconstructed program and the capability required by output writers.
//!
//! Planning has already chosen file placement, names, directives, evaluation
//! order, and vendor replacements. The syntax in this plan includes those
//! decisions. Final validation inspects that exact syntax; accepted plans expose
//! no mutation path, so emission cannot change the program after validation.
//! A gate-rejected program is frozen as a [`RejectedPlan`] instead, which only
//! the marked reading-only tree writer consumes.

use source_identity::ChunkId;
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use anyhow::{Context, Result, bail, ensure};
use artifact::EmissionFiles;
use artifact::reports::write_json;
use final_global_resolution::{
    GlobalResolutionReason, GlobalResolutionReport, GlobalResolutionSources,
    assess_final_global_resolution,
};
use gate::{ChunkGateRejection, UnmatchedSpecClaim};
use js_ast::GlobalUseElisions;
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
    files: EmissionFiles,
    vendor: Arc<VendorResolutionPlan>,
    vendor_manifest: Option<VendorManifestOutput>,
    unresolved_claims: Vec<UnmatchedSpecClaim>,
    browser_harness: Option<BrowserHarnessPlan>,
    global_resolution_sources: GlobalResolutionSources,
    global_use_elisions: BTreeMap<ChunkId, GlobalUseElisions>,
}

impl ReconstructionPlan {
    pub fn new(
        candidate: FinalizedCandidateEmissionDraft,
        global_resolution_sources: GlobalResolutionSources,
        unresolved_claims: Vec<UnmatchedSpecClaim>,
        browser_options: Option<&spec::EmitBrowserHarnessConfig>,
        vendor_manifest: Option<VendorManifestOutput>,
    ) -> Result<Self> {
        let global_use_elisions = candidate.global_use_elisions().clone();
        let (files, vendor) = candidate.into_files_and_vendor_plan();
        let browser_harness = browser_options
            .map(|options| {
                BrowserHarnessPlan::prepare(
                    files.files(),
                    &vendor.full_swap_chunk_ids(),
                    &vendor
                        .full_swap_resolutions()
                        .values()
                        .filter_map(|resolution| {
                            files
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
            files,
            vendor,
            vendor_manifest,
            unresolved_claims,
            browser_harness,
            global_resolution_sources,
            global_use_elisions,
        })
    }

    /// Validate the exact reconstructed syntax after all body transformations.
    /// Keep both post-reconstruction safeguards: synthesized directives are not
    /// fully covered by the input-space replacement checks.
    pub fn validate(self) -> Result<ValidatedPlan> {
        validate_partial_swap_consumers(&self.files, &self.vendor)?;
        let excluded_chunk_ids = self.vendor.full_swap_chunk_ids();
        validate_emitted_exports(&self.files, &excluded_chunk_ids)?;
        reject_unresolved_claims(&self.unresolved_claims)?;
        let global_resolution = assess_final_global_resolution(
            &self.global_resolution_sources,
            self.files.files(),
            &excluded_chunk_ids,
            &self.global_use_elisions,
        );
        let captures = global_resolution
            .chunks
            .iter()
            .flat_map(|(chunk, assessment)| {
                assessment.findings.iter().filter_map(move |finding| {
                    (finding.reason == GlobalResolutionReason::CapturedByBinding).then(|| {
                        format!(
                            "  - chunk {chunk}: source global `{}` captured in {}",
                            finding.symbol.as_deref().unwrap_or("<unknown>"),
                            finding.output_files.join(", "),
                        )
                    })
                })
            })
            .collect::<Vec<_>>();
        if !captures.is_empty() {
            bail!(
                "reconstructed program captures source global references:\n{}",
                captures.join("\n")
            );
        }
        Ok(ValidatedPlan {
            files: self.files,
            vendor: self.vendor,
            vendor_manifest: self.vendor_manifest,
            excluded_chunk_ids,
            browser_harness: self.browser_harness,
            global_resolution,
        })
    }
}

/// An immutable concrete plan accepted by the shared final validator.
/// Only `ReconstructionPlan::validate` can construct this capability.
pub struct ValidatedPlan {
    files: EmissionFiles,
    vendor: Arc<VendorResolutionPlan>,
    vendor_manifest: Option<VendorManifestOutput>,
    excluded_chunk_ids: BTreeSet<ChunkId>,
    browser_harness: Option<BrowserHarnessPlan>,
    global_resolution: GlobalResolutionReport,
}

impl ValidatedPlan {
    pub fn files(&self) -> &EmissionFiles {
        &self.files
    }

    pub fn excluded_chunk_ids(&self) -> &BTreeSet<ChunkId> {
        &self.excluded_chunk_ids
    }

    pub fn browser_harness(&self) -> Option<&BrowserHarnessPlan> {
        self.browser_harness.as_ref()
    }

    pub fn global_resolution(&self) -> &GlobalResolutionReport {
        &self.global_resolution
    }

    /// Write the replacement assets and manifest belonging to this accepted program.
    pub fn write_vendor_outputs(&self) -> Result<()> {
        write_planned_vendor_outputs(&self.vendor)?;
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
    files: EmissionFiles,
    excluded_chunk_ids: BTreeSet<ChunkId>,
    rejections: Vec<ChunkGateRejection>,
}

impl RejectedPlan {
    /// Fails, naming the gate's diagnostic too, on unresolved spec claims: a
    /// reading-only tree carries the gate's rejection and no other failure.
    pub fn new(
        candidate: FinalizedCandidateEmissionDraft,
        rejections: Vec<ChunkGateRejection>,
        unresolved_claims: &[UnmatchedSpecClaim],
    ) -> Result<Self> {
        let (files, vendor) = candidate.into_files_and_vendor_plan();
        ensure!(
            !rejections.is_empty(),
            "a rejected plan needs at least one gate rejection"
        );
        let plan = Self {
            files,
            excluded_chunk_ids: vendor.full_swap_chunk_ids(),
            rejections,
        };
        reject_unresolved_claims(unresolved_claims).with_context(|| plan.diagnostic())?;
        Ok(plan)
    }

    pub fn files(&self) -> &EmissionFiles {
        &self.files
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
