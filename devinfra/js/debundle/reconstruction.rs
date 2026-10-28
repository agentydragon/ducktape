//! Concrete reconstructed program and the capability required by output writers.
//!
//! Planning has already chosen file placement, names, directives, evaluation
//! order, and vendor replacements. The syntax in this plan includes those
//! decisions. Final validation inspects that exact syntax; accepted plans expose
//! no mutation path, so emission cannot change the program after validation.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use anyhow::{Result, bail};
use artifact::reports::write_json;
use artifact::{ChunkId, EmissionFiles};
use serde::Serialize;
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

/// A `define_logical_module` member whose `binding.name` did not
/// resolve to a top-level declaration in the chunk. Collected across chunks
/// and rejected at final validation before accepted output is written.
#[derive(Debug, Clone, Serialize)]
pub struct UnmatchedSpecClaim {
    pub chunk_id: String,
    /// Claiming module, by canonical [`spec::ModulePath`].
    pub module_path: spec::ModulePath,
    pub binding_name: String,
    pub export_name: String,
}

/// Ready-to-validate output syntax and replacement decisions for one program.
/// Diagnostic reports are separate from the accepted output tree.
pub struct ReconstructionPlan {
    files: EmissionFiles,
    vendor: Arc<VendorResolutionPlan>,
    vendor_manifest: Option<VendorManifestOutput>,
    unresolved_claims: Vec<UnmatchedSpecClaim>,
    browser_harness: Option<BrowserHarnessPlan>,
}

impl ReconstructionPlan {
    pub fn new(
        files: EmissionFiles,
        vendor: Arc<VendorResolutionPlan>,
        unresolved_claims: Vec<UnmatchedSpecClaim>,
        browser_options: Option<&spec::EmitBrowserHarnessConfig>,
        vendor_manifest: Option<VendorManifestOutput>,
    ) -> Result<Self> {
        let browser_harness = browser_options
            .map(|options| {
                BrowserHarnessPlan::prepare(files.files(), &vendor.full_swap_chunk_ids(), options)
            })
            .transpose()?;
        Ok(Self {
            files,
            vendor,
            vendor_manifest,
            unresolved_claims,
            browser_harness,
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
        Ok(ValidatedPlan {
            files: self.files,
            vendor: self.vendor,
            vendor_manifest: self.vendor_manifest,
            excluded_chunk_ids,
            browser_harness: self.browser_harness,
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

    /// Write the replacement assets and manifest belonging to this accepted program.
    pub fn write_vendor_outputs(&self) -> Result<()> {
        write_planned_vendor_outputs(&self.vendor)?;
        if let Some(manifest) = &self.vendor_manifest {
            write_json(&manifest.path, &manifest.report)?;
        }
        Ok(())
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
