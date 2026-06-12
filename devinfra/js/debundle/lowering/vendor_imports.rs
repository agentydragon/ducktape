//! Construction-time vendor consultation for materialized module
//! bodies: classify each planned
//! runtime re-import against the `VendorResolutionPlan` and, for
//! vendor-swapped targets, construct the package / facade import plus
//! the body-replacement map directly. Pass-through files (chunk
//! entries, runtime files) are the other application site, handled by
//! the unified emission rewriter (`vendor::passthrough`) consuming the
//! same plan.

use vendor::{
    DeferredImport, IdentRewriteTarget, MaterializedOutputChunkIndex, VendorImportAction,
    VendorResolutionPlan, bundled_facade_import_source, resolve_partial_swap_import_target,
};

use super::*;

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
pub(super) struct VendorReimportOracle<'a> {
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
    pub(super) fn new(
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
pub(super) struct PlannedVendorReimports<'a> {
    /// Re-imports the oracle did not claim — constructed as chunk
    /// re-imports exactly as before.
    pub(super) retained: BTreeMap<Id, &'a RuntimeImportInfo>,
    /// Boundary-rename name mapping for retained named re-imports
    /// (vendor-local → public). Load-bearing: source ASTs reach
    /// lowering with the vendor-local names, and the public name is
    /// applied at import construction.
    pub(super) imported_overrides: BTreeMap<Id, String>,
    /// External package / facade import decls replacing claimed
    /// re-imports; appended to the runtime re-import block.
    pub(super) external_imports: Vec<ModuleItem>,
    /// Body replacements for member-access / named-rename claimed
    /// bindings, applied by `PartialSwapIdentRewriter` after the sealed
    /// rename application (like the runtime-URL rewrite).
    pub(super) body_rewrites: BTreeMap<Id, IdentRewriteTarget>,
    /// Whole-import replacements already counted at construction
    /// (namespace / default / named-without-alias kinds), summed into
    /// the manifest's per-symbol `references_rewritten`.
    pub(super) references_rewritten: BTreeMap<(ChunkId, String), usize>,
}

pub(super) fn plan_vendor_reimports<'a>(
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
    // Shared-import dedupe per emitted file, mirroring the wave's
    // per-file `emitted_member_namespace_for` / `emitted_default_namespace_for`.
    let mut emitted_shared_import_for: BTreeSet<String> = BTreeSet::new();
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
        match action {
            VendorImportAction::PackageMember {
                package,
                namespace,
                upstream_export,
            } => {
                planned.body_rewrites.insert(
                    local_id,
                    IdentRewriteTarget::Member {
                        namespace: namespace.clone(),
                        upstream_export,
                        chunk_id: chunk,
                        chunk_export,
                    },
                );
                if emitted_shared_import_for.insert(format!("package:{package}")) {
                    planned.external_imports.push(
                        DeferredImport::Namespace {
                            source: package,
                            local: namespace,
                        }
                        .into_module_item(),
                    );
                }
            }
            VendorImportAction::PackageNamespace { package } => {
                planned.external_imports.push(
                    DeferredImport::Namespace {
                        source: package,
                        local: local_id.0.to_string(),
                    }
                    .into_module_item(),
                );
                *planned
                    .references_rewritten
                    .entry((chunk, chunk_export))
                    .or_insert(0) += 1;
            }
            VendorImportAction::PackageDefault { package } => {
                planned.external_imports.push(
                    DeferredImport::Default {
                        source: package,
                        local: local_id.0.to_string(),
                    }
                    .into_module_item(),
                );
                *planned
                    .references_rewritten
                    .entry((chunk, chunk_export))
                    .or_insert(0) += 1;
            }
            VendorImportAction::PackageNamed {
                package,
                upstream_export,
            } => {
                planned.external_imports.push(
                    DeferredImport::Named {
                        source: package,
                        local: upstream_export.clone(),
                        upstream_export: upstream_export.clone(),
                    }
                    .into_module_item(),
                );
                if local_id.0.as_ref() != upstream_export {
                    planned.body_rewrites.insert(
                        local_id,
                        IdentRewriteTarget::Rename {
                            upstream_export,
                            chunk_id: chunk,
                            chunk_export,
                        },
                    );
                } else {
                    *planned
                        .references_rewritten
                        .entry((chunk, chunk_export))
                        .or_insert(0) += 1;
                }
            }
            VendorImportAction::FacadeMember {
                package,
                facade_app_path,
                namespace,
                upstream_export,
            } => {
                let source = bundled_facade_import_source(
                    oracle.chunk_table,
                    source_chunk_id,
                    target_file,
                    &facade_app_path,
                );
                planned.body_rewrites.insert(
                    local_id,
                    IdentRewriteTarget::Member {
                        namespace: namespace.clone(),
                        upstream_export,
                        chunk_id: chunk,
                        chunk_export,
                    },
                );
                if emitted_shared_import_for.insert(format!("facade:{package}")) {
                    planned.external_imports.push(
                        DeferredImport::Default {
                            source,
                            local: namespace,
                        }
                        .into_module_item(),
                    );
                }
            }
            VendorImportAction::FacadeDefault { facade_app_path } => {
                let source = bundled_facade_import_source(
                    oracle.chunk_table,
                    source_chunk_id,
                    target_file,
                    &facade_app_path,
                );
                planned.external_imports.push(
                    DeferredImport::Default {
                        source,
                        local: local_id.0.to_string(),
                    }
                    .into_module_item(),
                );
                *planned
                    .references_rewritten
                    .entry((chunk, chunk_export))
                    .or_insert(0) += 1;
            }
        }
    }
    planned
}
