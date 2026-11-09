//! Ownership of the complete post-materialization emission sequence.
//!
//! A draft is created only by the canonical materializer or the pass-through
//! constructor. It owns the emitted files while the fixed whole-program
//! rewrite stages run, then hands an immutable finalized product to
//! reconstruction's validator.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use anyhow::Result;
use artifact::reports::SelectedModuleLowering;
use artifact::{EmissionFiles, SourceProgram};
use source_identity::ChunkId;
use vendor::{VendorResolutionPlan, apply_emission_rewrites_in_place};

use super::naturalize_cross_chunk_imports;

/// Canonical materialization output before whole-program rewrites. The only
/// operation consumes the draft and runs every final rewrite in order.
pub struct CandidateEmissionDraft {
    files: EmissionFiles,
    vendor_plan: Arc<VendorResolutionPlan>,
    selected_lowerings: Vec<SelectedModuleLowering>,
    materialized_chunks: BTreeSet<String>,
    /// Vendor rewrites lowering applied at construction time inside
    /// materialized module bodies, keyed by (swapped chunk, chunk export).
    vendor_reference_rewrites: BTreeMap<(ChunkId, String), usize>,
}

/// The emitted files after naturalization, vendor rewrites, and both prune
/// stages. No mutable file access remains; reconstruction consumes it.
pub struct FinalizedCandidateEmissionDraft {
    files: EmissionFiles,
    vendor_plan: Arc<VendorResolutionPlan>,
    references_by_symbol: BTreeMap<(ChunkId, String), usize>,
}

impl CandidateEmissionDraft {
    pub(super) fn materialized(
        files: EmissionFiles,
        vendor_plan: Arc<VendorResolutionPlan>,
        selected_lowerings: Vec<SelectedModuleLowering>,
        materialized_chunks: BTreeSet<String>,
        vendor_reference_rewrites: BTreeMap<(ChunkId, String), usize>,
    ) -> Self {
        Self {
            files,
            vendor_plan,
            selected_lowerings,
            materialized_chunks,
            vendor_reference_rewrites,
        }
    }

    /// A run with no materialized chunks emits the prepared source program.
    pub fn pass_through(
        source_program: SourceProgram,
        vendor_plan: Arc<VendorResolutionPlan>,
    ) -> Self {
        Self {
            files: EmissionFiles::from_prepared(source_program),
            vendor_plan,
            selected_lowerings: Vec::new(),
            materialized_chunks: BTreeSet::new(),
            vendor_reference_rewrites: BTreeMap::new(),
        }
    }

    /// Run the fixed whole-program rewrite sequence.
    pub fn finalize(self) -> Result<FinalizedCandidateEmissionDraft> {
        let Self {
            files,
            vendor_plan,
            selected_lowerings,
            materialized_chunks,
            vendor_reference_rewrites,
        } = self;
        // Cross-chunk import naturalization runs first, before the emission
        // rewrites canonicalize source specifiers. Both change AST bodies
        // without changing the indexed output file set. The emission
        // rewrites are one artifact pass over two disjoint file sets:
        //
        // * the unified pass-through directive rewrite over files emitted
        //   without lowering in non-vendor chunks — specifier
        //   canonicalization, boundary-rename name mapping, and
        //   partial-swap consumer surgery from the vendor plan
        //   (materialized module bodies were constructed from the same
        //   plan during lowering; suppress-marked chunks are skipped
        //   wholesale; excluded full-swap chunks are never rewritten);
        // * the vendor residual computation for each partially-swapped
        //   chunk — the canonicalize → self-rewrite → strip composition as
        //   one function body.
        //
        // Runs after materialization so the per-symbol consumer rewrite
        // cannot erase binding names spec selectors matched on.
        let (files, emission_references) = files.rewrite_bodies(|artifact, indexes| {
            naturalize_cross_chunk_imports(
                artifact,
                indexes,
                &selected_lowerings,
                &materialized_chunks,
            )?;
            apply_emission_rewrites_in_place(artifact, &vendor_plan, indexes)
        })?;
        let mut references_by_symbol = vendor_reference_rewrites;
        for (key, count) in emission_references {
            *references_by_symbol.entry(key).or_insert(0) += count;
        }

        // Trim dead named import specifiers across the whole bundle (entry
        // included): the lowerer over-imports residual/hub bindings the
        // importing file never references. Runs immediately before the
        // export prune so that dropping a dead import — which the
        // name-based export prune would otherwise treat as a live consumer —
        // cascades into dropping the now-unimported module exports. The
        // export prune drops module-owned bindings referenced nowhere outside
        // their own module from the module's `export { ... }`; entry files
        // (the chunk public surface) are never touched. Soundness arguments:
        // prune_dead_imports.rs and prune_module_exports.rs.
        let (files, ()) = files.rewrite_bodies(|files, _| {
            prune_dead_imports::prune_dead_import_specifiers(files);
            prune_module_exports::prune_unimported_module_exports(files);
            Ok(())
        })?;

        Ok(FinalizedCandidateEmissionDraft {
            files,
            vendor_plan,
            references_by_symbol,
        })
    }
}

impl FinalizedCandidateEmissionDraft {
    pub fn files(&self) -> &EmissionFiles {
        &self.files
    }

    /// Vendor-swap rewrite counts across both application sites: module
    /// construction during lowering and the whole-program emission rewrite.
    pub fn references_by_symbol(&self) -> &BTreeMap<(ChunkId, String), usize> {
        &self.references_by_symbol
    }

    /// Consume the finalized product for reconstruction. No caller can
    /// rewrite the files after this point.
    pub fn into_files_and_vendor_plan(self) -> (EmissionFiles, Arc<VendorResolutionPlan>) {
        (self.files, self.vendor_plan)
    }
}
