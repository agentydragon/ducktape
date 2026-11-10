//! Construction-time vendor consultation for materialized module
//! bodies: classify each planned
//! runtime re-import against the `VendorResolutionPlan` and, for
//! vendor-swapped targets, construct the package / facade import plus
//! the body-replacement map directly. Pass-through files (chunk
//! entries, runtime files) are the other application site, handled by
//! the unified emission rewriter (`vendor::passthrough`) consuming the
//! same plan.

use source_identity::{ChunkId, ChunkTable};
use vendor::{
    DeferredImport, IdentRewriteTarget, MaterializedOutputChunkIndex, VendorImportAction,
    VendorImportRewrites, VendorResolutionPlan, bundled_facade_import_source,
    resolve_partial_swap_import_target,
};

use artifact::ArtifactIndexes;
use frontend_elaboration::source_ast::{RuntimeImportInfo, RuntimeImportKind};
use std::collections::BTreeMap;
use swc_ecma_ast::*;

/// Planner-boundary target vocabulary for one runtime re-import. Intra-chunk import lists stay keyed by
/// dense `ModuleId`s — the gate's universe never sees vendor targets —
/// so the design sketch's `Module(ModuleId)` variant is deferred until
/// a list actually mixes module and external targets.
enum ImportTarget {
    /// Cross-chunk artifact target: construct the chunk re-import as
    /// before.
    Chunk(ChunkId),
    /// Vendor-swapped target: construct the external package / facade
    /// import and body replacement from the plan oracle.
    External {
        chunk: ChunkId,
        chunk_export: String,
        action: VendorImportAction,
    },
}

/// Chunk-independent resolution context for classifying runtime
/// re-imports against the vendor plan; built once per
/// `materialize_logical_modules` run and shared by the per-chunk
/// lowering workers.
pub(crate) struct VendorReimportOracle<'a> {
    plan: &'a VendorResolutionPlan,
    chunk_table: &'a ChunkTable,
    references: &'a ArtifactIndexes,
    materialized_index: MaterializedOutputChunkIndex,
}

impl<'a> VendorReimportOracle<'a> {
    /// `None` when the plan carries no partial / bundled swaps and no
    /// boundary renames — classification would never fire. (Full-swap
    /// marks are covered: every `swap` mark contributes a
    /// boundary-rename plan entry.)
    pub(crate) fn new(
        plan: &'a VendorResolutionPlan,
        chunk_table: &'a ChunkTable,
        references: &'a ArtifactIndexes,
    ) -> Option<Self> {
        if !plan.has_partial_swaps()
            && !plan.has_bundled_partial_swaps()
            && !plan.has_boundary_renames()
        {
            return None;
        }
        Some(Self {
            plan,
            chunk_table,
            references,
            materialized_index: MaterializedOutputChunkIndex::build(chunk_table),
        })
    }

    /// Classify one runtime re-import by the chunk its source directive
    /// targets, resolved from the source chunk's own coordinate system —
    /// the same resolution the pass-through emission rewriter performs
    /// on the rebased specifier in an emitted file (the rebase in
    /// `source_chunk_imports_for_moved_body` is exactly the coordinate
    /// change between the two). `None`: not a chunk target (bare
    /// package specifier or extra-file asset).
    fn classify(
        &self,
        info: &RuntimeImportInfo,
        source_chunk_id: ChunkId,
        source_entry_file: &str,
    ) -> Option<ImportTarget> {
        let target = resolve_partial_swap_import_target(
            &info.src,
            source_chunk_id,
            source_entry_file,
            self.references,
            self.chunk_table,
            &self.materialized_index,
        )?;
        if target != source_chunk_id
            && let RuntimeImportKind::Named { imported } = &info.kind
            && let Some(action) = self.plan.swapped_named_import_action(target, imported)
        {
            return Some(ImportTarget::External {
                chunk: target,
                chunk_export: imported.clone(),
                action,
            });
        }
        Some(ImportTarget::Chunk(target))
    }
}

/// The vendor-consulted split of a module body's runtime re-imports.
pub(crate) struct PlannedVendorReimports<'a> {
    /// Re-imports the oracle did not claim — constructed as chunk
    /// re-imports exactly as before.
    pub(crate) retained: BTreeMap<Id, &'a RuntimeImportInfo>,
    /// Boundary-rename name mapping for retained named re-imports
    /// (vendor-local → public). Load-bearing: source ASTs reach
    /// lowering with the vendor-local names, and the public name is
    /// applied at import construction.
    pub(crate) imported_overrides: BTreeMap<Id, String>,
    /// External package / facade import decls replacing claimed
    /// re-imports; appended to the runtime re-import block.
    pub(crate) external_imports: Vec<ModuleItem>,
    /// Body replacements for member-access / named-rename claimed
    /// bindings, applied by `PartialSwapIdentRewriter` after the sealed
    /// rename application (like the runtime-URL rewrite).
    pub(crate) body_rewrites: BTreeMap<Id, IdentRewriteTarget>,
    /// Whole-import replacements already counted at construction
    /// (namespace / default / named-without-alias kinds), summed into
    /// the manifest's per-symbol `references_rewritten`.
    pub(crate) references_rewritten: BTreeMap<(ChunkId, String), usize>,
}

pub(crate) fn plan_vendor_reimports<'a>(
    needed: BTreeMap<Id, &'a RuntimeImportInfo>,
    oracle: Option<&VendorReimportOracle<'_>>,
    source_chunk_id: ChunkId,
    source_entry_file: &str,
    target_file: &str,
) -> PlannedVendorReimports<'a> {
    let mut planned = PlannedVendorReimports {
        retained: BTreeMap::new(),
        imported_overrides: BTreeMap::new(),
        external_imports: Vec::new(),
        body_rewrites: BTreeMap::new(),
        references_rewritten: BTreeMap::new(),
    };
    let Some(oracle) = oracle else {
        planned.retained = needed;
        return planned;
    };
    let mut rewrites = VendorImportRewrites::default();
    for (local_id, info) in needed {
        let (chunk, chunk_export, action) =
            match oracle.classify(info, source_chunk_id, source_entry_file) {
                Some(ImportTarget::External {
                    chunk,
                    chunk_export,
                    action,
                }) => (chunk, chunk_export, action),
                Some(ImportTarget::Chunk(chunk)) => {
                    if chunk != source_chunk_id
                        && let RuntimeImportKind::Named { imported } = &info.kind
                        && let Some(public) =
                            oracle.plan.boundary_public_export_name(chunk, imported)
                        && public != imported
                    {
                        planned
                            .imported_overrides
                            .insert(local_id.clone(), public.to_string());
                    }
                    planned.retained.insert(local_id, info);
                    continue;
                }
                None => {
                    planned.retained.insert(local_id, info);
                    continue;
                }
            };
        planned.external_imports.extend(
            rewrites
                .materialize(action, local_id, chunk, chunk_export, |path| {
                    bundled_facade_import_source(
                        oracle.chunk_table,
                        source_chunk_id,
                        target_file,
                        path,
                    )
                })
                .into_iter()
                .map(DeferredImport::into_module_item),
        );
    }
    planned.body_rewrites = rewrites.body_rewrites;
    planned.references_rewritten = rewrites.references_rewritten;
    planned
}
