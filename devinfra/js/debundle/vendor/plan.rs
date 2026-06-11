//! Read-only vendor resolution plan: the single post-prepare resolution
//! oracle for every vendor application site.
//!
//! `build_vendor_resolution_plan` runs once, immediately after chunk
//! preparation. It validates every vendor mark against the artifact in
//! one pass (unknown chunk / missing entry, boundary-mapping collisions,
//! wrapper-shape + `default_export_aliases` soundness, partial-swap
//! symbol/package shape, installed-package versions, consumer-shape
//! classification) and records the resolved decisions: boundary
//! mappings, full-swap wrapper text and output paths, partial/bundled
//! swap symbol tables and facade targets, plus the wire-facing
//! resolution projections the vendor manifests are built from. The
//! application sites (`swap_vendor_chunks`, lowering's import
//! construction, the pass-through emission rewriter, the bundled
//! self-rewrite, strip) consume the plan instead of re-resolving marks
//! mid-pipeline; application (directive rewrites, file writes, chunk
//! removal, strip) stays at those sites.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use rayon::prelude::*;
use serde_json::Value;
use swc_common::GLOBALS;
use swc_ecma_ast::{ExportSpecifier, ImportSpecifier, ModuleDecl, ModuleItem};

use artifact::{
    ArtifactIndexes, ChunkBundle, ChunkId, get_chunk_entry_path, join_module_path,
    list_chunk_file_paths, manifest_relative_path,
};
use binding_targets::module_export_name;
use js_ast::{parse_js_module, str_value};
use spec::{
    PartialSwapKind, PartialSwapPackage, PartialSwapSymbol, VendorLevel, VendorMark, WrapperShape,
};

use crate::manifests::{
    BundledPartialSwapBundleResolution, BundledPartialSwapPackageResolution,
    ChunkBundledPartialSwapResolution, ChunkPartialSwapResolution, PartialSwapPackageResolution,
    VendorResolution,
};
use crate::validate::{
    PartialSwapPackageCoords, ResolvePartialSwapPackageOptions, ResolvedVendorChunk,
    build_partial_swap_symbol_resolutions, resolve_partial_swap_package,
    validate_partial_swap_symbols, vendor_chunk_name, vendor_entry_ast,
};
use crate::wrappers::{
    PlannedBundledAssets, generate_named_from_default_wrapper,
    generate_named_from_json_default_wrapper, generate_named_from_module_default_wrapper,
    plan_bundled_partial_swap_assets, set_diff, wrapper_output_path,
};
use crate::{
    MaterializedOutputChunkIndex, collect_boundary_mapping, collect_default_export_object_keys,
    collect_exported_names, is_valid_identifier, module_has_export_star,
    read_installed_package_metadata, resolve_package_subpath, resolve_partial_swap_import_target,
    validate_boundary_mapping_collisions, verified_default_alias_export_names,
};

#[derive(Debug, Clone)]
pub struct VendorPlanOptions<'a> {
    pub package_roots: &'a HashMap<String, PathBuf>,
    pub packages_root: &'a Option<PathBuf>,
    /// Manifest path generated wrapper / facade paths are recorded
    /// relative to in the wire resolutions.
    pub output_manifest_path: Option<&'a Path>,
    /// Output directory wrappers and facade bundles are planned under.
    pub output_wrapper_dir: Option<&'a Path>,
}

pub struct VendorResolutionPlan {
    /// `boundary_rename` + `swap` marks in chunk-path order, each with
    /// its vendor-local → public export mapping (possibly empty).
    pub(crate) boundary_renames: Vec<BoundaryRenamePlan>,
    pub(crate) full_swaps: Vec<FullSwapPlan>,
    pub(crate) partial_swaps: BTreeMap<ChunkId, ChunkPartialSwapPlan>,
    pub(crate) bundled_partial_swaps: BTreeMap<ChunkId, ChunkBundledPartialSwapPlan>,
    /// `suppress`-marked chunks: hands-off for every rewrite — the
    /// pass-through emission rewriter skips their files entirely, so
    /// their emitted directives stay byte-identical to the prepared
    /// input (vendor_into_emission open question 3).
    pub(crate) suppressed: BTreeSet<ChunkId>,
}

impl VendorResolutionPlan {
    pub fn has_boundary_renames(&self) -> bool {
        !self.boundary_renames.is_empty()
    }

    pub fn has_full_swaps(&self) -> bool {
        !self.full_swaps.is_empty()
    }

    pub fn has_partial_swaps(&self) -> bool {
        !self.partial_swaps.is_empty()
    }

    pub fn has_bundled_partial_swaps(&self) -> bool {
        !self.bundled_partial_swaps.is_empty()
    }

    /// Oracle answer for one named import of `imported_name` targeting
    /// `chunk`: how lowering's import construction must materialize it.
    /// Mirrors the post-materialize dispatchers' per-specifier
    /// classification exactly — `None` means "keep the chunk
    /// re-import" (the post-strip consumer gate stays the safety net
    /// for shapes with no live rewrite, e.g. a symbol whose package
    /// coordinates are missing the required `namespace`).
    pub fn swapped_named_import_action(
        &self,
        chunk: ChunkId,
        imported_name: &str,
    ) -> Option<VendorImportAction> {
        if let Some(partial) = self.partial_swaps.get(&chunk) {
            let symbol = partial.symbols.get(imported_name)?;
            let coords = partial.packages.get(&symbol.package)?;
            return Some(match symbol.kind {
                PartialSwapKind::Member => VendorImportAction::PackageMember {
                    package: symbol.package.clone(),
                    namespace: coords.namespace.clone()?,
                    upstream_export: symbol.upstream_export.clone()?,
                },
                PartialSwapKind::Namespace => VendorImportAction::PackageNamespace {
                    package: symbol.package.clone(),
                },
                PartialSwapKind::Default => VendorImportAction::PackageDefault {
                    package: symbol.package.clone(),
                },
                PartialSwapKind::Named => VendorImportAction::PackageNamed {
                    package: symbol.package.clone(),
                    upstream_export: symbol.upstream_export.clone()?,
                },
            });
        }
        let bundled = self.bundled_partial_swaps.get(&chunk)?;
        let symbol = bundled.symbols.get(imported_name)?;
        let target = bundled.packages.get(&symbol.package)?;
        Some(match symbol.kind {
            PartialSwapKind::Member | PartialSwapKind::Named => VendorImportAction::FacadeMember {
                package: symbol.package.clone(),
                facade_app_path: target.facade_app_path.clone(),
                namespace: target.namespace.clone()?,
                upstream_export: symbol.upstream_export.clone()?,
            },
            PartialSwapKind::Namespace | PartialSwapKind::Default => {
                VendorImportAction::FacadeDefault {
                    facade_app_path: target.facade_app_path.clone(),
                }
            }
        })
    }

    /// Boundary-rename mapping consult for construction-time naming
    /// (vendor_into_emission §2.4): vendor-local export name → public
    /// name for a `boundary_rename` / `swap` chunk. Load-bearing since
    /// the pre-materialize `rename_vendor_exports` wave was deleted:
    /// source ASTs reach lowering with the vendor-local names, and the
    /// public name is applied at import construction.
    pub fn boundary_public_export_name(&self, chunk: ChunkId, vendor_local: &str) -> Option<&str> {
        self.boundary_renames
            .iter()
            .find(|plan| plan.chunk_id == chunk)
            .and_then(|plan| plan.mapping.get(vendor_local))
            .map(String::as_str)
    }

    /// Output-tree path (`<chunk_name>/<entry_file>`) of a fully-swapped
    /// chunk's entry. The chunk is removed from the artifact by
    /// `swap_vendor_chunks`, so index resolution cannot canonicalize
    /// directives that keep targeting it (the live-proxy dangling-import
    /// contract); construction and the pass-through rewriter consult
    /// this instead.
    pub fn full_swap_target_path(&self, chunk: ChunkId) -> Option<String> {
        self.full_swaps
            .iter()
            .find(|swap| swap.chunk_id == chunk)
            .map(|swap| join_module_path(&[&swap.resolution.chunk_id, &swap.resolution.entry_file]))
    }

    pub fn is_suppressed(&self, chunk: ChunkId) -> bool {
        self.suppressed.contains(&chunk)
    }

    /// Entry file of a `boundary_rename` / `swap` chunk — the only file
    /// whose named imports the boundary mapping applies to.
    pub(crate) fn boundary_entry_file(&self, chunk: ChunkId) -> Option<&str> {
        self.boundary_renames
            .iter()
            .find(|plan| plan.chunk_id == chunk)
            .map(|plan| plan.entry_file.as_str())
    }
}

/// How a vendor-swapped named import must be constructed in a lowered
/// module body. Variants mirror [`spec::PartialSwapKind`] split by the
/// partial (raw package specifier) vs bundled (generated facade)
/// families.
#[derive(Debug, Clone)]
pub enum VendorImportAction {
    /// partial `kind=member`: one shared
    /// `import * as <namespace> from "<package>"` per file per package,
    /// references rewritten to `<namespace>.<upstream_export>`.
    PackageMember {
        package: String,
        namespace: String,
        upstream_export: String,
    },
    /// partial `kind=namespace`: `import * as <local> from "<package>"`.
    PackageNamespace { package: String },
    /// partial `kind=default`: `import <local> from "<package>"`.
    PackageDefault { package: String },
    /// partial `kind=named`: `import { <upstream_export> } from
    /// "<package>"`; aliased locals are renamed to the bare upstream
    /// name in the body.
    PackageNamed {
        package: String,
        upstream_export: String,
    },
    /// bundled `kind=member|named`: one shared
    /// `import <namespace> from "<facade>"` per file per package,
    /// references rewritten to `<namespace>.<upstream_export>`.
    FacadeMember {
        package: String,
        facade_app_path: String,
        namespace: String,
        upstream_export: String,
    },
    /// bundled `kind=namespace|default`:
    /// `import <local> from "<facade>"`.
    FacadeDefault { facade_app_path: String },
}

pub(crate) struct BoundaryRenamePlan {
    pub(crate) chunk_id: ChunkId,
    pub(crate) entry_file: String,
    /// Vendor-local binding name → public export name.
    pub(crate) mapping: BTreeMap<String, String>,
}

pub(crate) struct FullSwapPlan {
    pub(crate) chunk_id: ChunkId,
    /// The chunk's export surface, consumed by the swap wave's
    /// import-alignment check (which runs post-rename against the
    /// then-current indexes).
    pub(crate) vendor_exports: BTreeSet<String>,
    /// Generated wrapper text + absolute output path; written during
    /// the swap wave when writes are enabled.
    pub(crate) wrapper: Option<PlannedWrapper>,
    /// Wire-facing resolution projected from this plan entry.
    pub(crate) resolution: VendorResolution,
}

pub(crate) struct PlannedWrapper {
    pub(crate) abs_path: PathBuf,
    pub(crate) source: String,
}

pub(crate) struct ChunkPartialSwapPlan {
    pub(crate) chunk_path: String,
    pub(crate) entry_file: String,
    /// package_name → upstream coords (namespace, version, subpath).
    pub(crate) packages: BTreeMap<String, PartialSwapPackage>,
    /// chunk_export → which package + how to rewrite it.
    pub(crate) symbols: BTreeMap<String, PartialSwapSymbol>,
    /// Wire-facing resolution skeleton (zero-initialized rewrite counts).
    pub(crate) resolution: ChunkPartialSwapResolution,
}

pub(crate) struct ChunkBundledPartialSwapPlan {
    pub(crate) chunk_path: String,
    pub(crate) entry_file: String,
    /// package_name → namespace + generated facade target.
    pub(crate) packages: BTreeMap<String, BundledPartialSwapPackageTarget>,
    /// chunk_export → which package + how to rewrite it.
    pub(crate) symbols: BTreeMap<String, PartialSwapSymbol>,
    /// Bundle copy + per-package facades; written during the bundled
    /// wave when writes are enabled.
    pub(crate) assets: PlannedBundledAssets,
    /// Wire-facing resolution skeleton (zero-initialized rewrite counts).
    pub(crate) resolution: ChunkBundledPartialSwapResolution,
}

#[derive(Debug, Clone)]
pub(crate) struct BundledPartialSwapPackageTarget {
    pub(crate) namespace: Option<String>,
    pub(crate) facade_app_path: String,
}

pub fn build_vendor_resolution_plan(
    artifact: &ChunkBundle,
    references: &ArtifactIndexes,
    vendor: &BTreeMap<String, VendorMark>,
    options: &VendorPlanOptions<'_>,
) -> Result<VendorResolutionPlan> {
    // Mark validation: every vendor entry must name a known chunk with
    // an entry file, regardless of level. One consolidated pass; later
    // phases reuse the resolved chunks.
    let mut resolved_chunks: BTreeMap<&str, ResolvedVendorChunk> = BTreeMap::new();
    for chunk_path in vendor.keys() {
        let chunk_name = vendor_chunk_name(chunk_path, "mark_vendor")?;
        let chunk_id = artifact.chunk_table.get(&chunk_name).with_context(|| {
            format!("vendor entry {chunk_path} targets unknown chunk: {chunk_name}")
        })?;
        let entry_file = get_chunk_entry_path(artifact, chunk_id).with_context(|| {
            format!("vendor entry {chunk_path} targets missing chunk (chunk_id={chunk_name})")
        })?;
        resolved_chunks.insert(
            chunk_path.as_str(),
            ResolvedVendorChunk {
                chunk_id,
                chunk_name,
                entry_file,
            },
        );
    }

    let suppressed = vendor
        .iter()
        .filter(|(_, mark)| matches!(mark.level, VendorLevel::Suppress))
        .map(|(chunk_path, _)| resolved_chunks[chunk_path.as_str()].chunk_id)
        .collect();
    let plan = VendorResolutionPlan {
        boundary_renames: plan_boundary_renames(artifact, vendor, &resolved_chunks)?,
        full_swaps: plan_full_swaps(artifact, vendor, &resolved_chunks, options)?,
        partial_swaps: plan_partial_swaps(artifact, vendor, &resolved_chunks, options)?,
        bundled_partial_swaps: plan_bundled_partial_swaps(
            artifact,
            vendor,
            &resolved_chunks,
            options,
        )?,
        suppressed,
    };
    validate_consumer_shapes(artifact, references, &plan)?;
    Ok(plan)
}

/// Plan-time consumer gate (vendor_into_emission §3.2): enumerate every
/// consumer directive targeting a partially-swapped chunk and reject the
/// shapes that have no live rewrite at either application site — before
/// any output is written. The classification is the same one the
/// rewriters perform, so a consumer the rewriter misses cannot pass the
/// gate. The post-strip `validate_partial_swap_consumers` scan stays on
/// as a differential tripwire (it additionally covers directives that
/// lowering moves or synthesizes inside materialized module bodies)
/// until PR 6 retires it with fixture evidence.
///
/// Over-restriction is the accepted failure mode: a namespace consumer
/// reading only unswapped members is still rejected.
fn validate_consumer_shapes(
    artifact: &ChunkBundle,
    references: &ArtifactIndexes,
    plan: &VendorResolutionPlan,
) -> Result<()> {
    struct SwappedChunk<'a> {
        symbols: &'a BTreeMap<String, PartialSwapSymbol>,
        bundled: bool,
    }
    let swapped_by_chunk: BTreeMap<ChunkId, SwappedChunk<'_>> = plan
        .partial_swaps
        .iter()
        .map(|(chunk_id, partial)| {
            (
                *chunk_id,
                SwappedChunk {
                    symbols: &partial.symbols,
                    bundled: false,
                },
            )
        })
        .chain(
            plan.bundled_partial_swaps
                .iter()
                .map(|(chunk_id, bundled)| {
                    (
                        *chunk_id,
                        SwappedChunk {
                            symbols: &bundled.symbols,
                            bundled: true,
                        },
                    )
                }),
        )
        .collect();
    let boundary_mapped_chunks: BTreeMap<ChunkId, &BTreeMap<String, String>> = plan
        .boundary_renames
        .iter()
        .filter(|boundary| !boundary.mapping.is_empty())
        .map(|boundary| (boundary.chunk_id, &boundary.mapping))
        .collect();
    if swapped_by_chunk.is_empty() && boundary_mapped_chunks.is_empty() {
        return Ok(());
    }
    let chunk_table = &artifact.chunk_table;
    let materialized_index = MaterializedOutputChunkIndex::build(chunk_table);
    // Full-swap chunks are removed from the artifact before any
    // consumer rewrite runs; their files are never emitted, so their
    // directives are not consumers (the post-strip scan never saw them
    // either).
    let full_swap_chunks: BTreeSet<ChunkId> =
        plan.full_swaps.iter().map(|swap| swap.chunk_id).collect();
    for chunk_artifact in &artifact.chunks {
        let caller_chunk_id = chunk_artifact.chunk_id;
        if full_swap_chunks.contains(&caller_chunk_id) {
            continue;
        }
        let caller_suppressed = plan.is_suppressed(caller_chunk_id);
        let caller_chunk_name = chunk_table.name(caller_chunk_id);
        for file_path in list_chunk_file_paths(&chunk_artifact.js) {
            let Some(ast) = chunk_artifact
                .js
                .get_file(&file_path)
                .and_then(|file| file.ast())
            else {
                continue;
            };
            for item in &ast.module.body {
                let ModuleItem::ModuleDecl(decl) = item else {
                    continue;
                };
                let source = match decl {
                    ModuleDecl::Import(import) => str_value(&import.src),
                    ModuleDecl::ExportNamed(named) => {
                        let Some(src) = named.src.as_deref() else {
                            continue;
                        };
                        str_value(src)
                    }
                    ModuleDecl::ExportAll(export_all) => str_value(&export_all.src),
                    _ => continue,
                };
                let Some(target_chunk_id) = resolve_partial_swap_import_target(
                    &source,
                    caller_chunk_id,
                    &file_path,
                    references,
                    chunk_table,
                    &materialized_index,
                ) else {
                    continue;
                };
                let consumer = format!("{caller_chunk_name}/{file_path}");
                // Boundary-renamed names are applied by the pass-through
                // rewriter (and by lowering's import construction);
                // suppress files are skipped by both, so a suppress
                // consumer spelling a vendor-local name would emit a
                // dangling import name.
                if caller_suppressed
                    && target_chunk_id != caller_chunk_id
                    && let Some(mapping) = boundary_mapped_chunks.get(&target_chunk_id)
                    && let ModuleDecl::Import(import) = decl
                {
                    for specifier in &import.specifiers {
                        let ImportSpecifier::Named(named) = specifier else {
                            continue;
                        };
                        let imported = named
                            .imported
                            .as_ref()
                            .map(module_export_name)
                            .unwrap_or_else(|| named.local.sym.to_string());
                        if let Some(public) = mapping.get(&imported)
                            && public != &imported
                        {
                            bail!(
                                "partial-swap consumer gate: {consumer} is in a suppress-marked chunk and imports vendor-local name `{imported}` from boundary-renamed vendor chunk {}; suppress files are hands-off, so the import would not be renamed to the public export `{public}`",
                                chunk_table.name(target_chunk_id),
                            );
                        }
                    }
                }
                let Some(swapped) = swapped_by_chunk.get(&target_chunk_id) else {
                    continue;
                };
                check_consumer_shape_has_live_rewrite(
                    decl,
                    swapped.symbols,
                    swapped.bundled,
                    caller_suppressed,
                    &consumer,
                    chunk_table.name(target_chunk_id),
                )?;
            }
        }
    }
    Ok(())
}

fn check_consumer_shape_has_live_rewrite(
    decl: &ModuleDecl,
    symbols: &BTreeMap<String, PartialSwapSymbol>,
    bundled: bool,
    caller_suppressed: bool,
    consumer: &str,
    target_chunk_name: &str,
) -> Result<()> {
    let swapped_list = || symbols.keys().cloned().collect::<Vec<_>>().join(",");
    match decl {
        ModuleDecl::Import(import) => {
            for specifier in &import.specifiers {
                match specifier {
                    ImportSpecifier::Named(named) => {
                        let imported = named
                            .imported
                            .as_ref()
                            .map(module_export_name)
                            .unwrap_or_else(|| named.local.sym.to_string());
                        if !symbols.contains_key(&imported) {
                            continue;
                        }
                        // Named imports of swapped names have a live
                        // rewrite at both application sites — unless the
                        // consumer file is hands-off (suppress chunk).
                        if caller_suppressed {
                            bail!(
                                "partial-swap consumer gate: {consumer} is in a suppress-marked chunk and imports swapped name `{imported}` from partially-swapped vendor chunk {target_chunk_name}; suppress files are not rewritten and the stripped chunk no longer exports it",
                            );
                        }
                    }
                    ImportSpecifier::Namespace(_) => {
                        bail!(
                            "partial-swap consumer gate: {consumer} namespace-imports partially-swapped vendor chunk {target_chunk_name}; swapped members [{}] would read as `undefined` on the namespace object — namespace consumers of partially-swapped chunks are unsupported, restructure the spec",
                            swapped_list(),
                        );
                    }
                    ImportSpecifier::Default(_) => {
                        if symbols.contains_key("default") {
                            bail!(
                                "partial-swap consumer gate: {consumer} default-imports partially-swapped vendor chunk {target_chunk_name} whose `default` export was swapped",
                            );
                        }
                    }
                }
            }
        }
        ModuleDecl::ExportNamed(named) => {
            for specifier in &named.specifiers {
                match specifier {
                    ExportSpecifier::Named(named_spec) => {
                        let orig = module_export_name(&named_spec.orig);
                        let Some(symbol) = symbols.get(&orig) else {
                            continue;
                        };
                        let exported = named_spec
                            .exported
                            .as_ref()
                            .map(module_export_name)
                            .unwrap_or_else(|| orig.clone());
                        if caller_suppressed {
                            bail!(
                                "partial-swap consumer gate: {consumer} is in a suppress-marked chunk and re-exports swapped name `{orig}` from partially-swapped vendor chunk {target_chunk_name}; suppress files are not rewritten and the stripped chunk no longer exports it",
                            );
                        }
                        if bundled || matches!(symbol.kind, PartialSwapKind::Member) {
                            bail!(
                                "partial-swap consumer gate: {consumer} re-exports swapped name `{orig}` from partially-swapped vendor chunk {target_chunk_name}; this re-export shape has no live rewrite (kind=member symbols and bundled swaps cannot be expressed as re-exports) and the stripped chunk no longer exports it",
                            );
                        }
                        if !is_valid_identifier(&exported) {
                            bail!(
                                "partial-swap consumer gate: {consumer} re-exports swapped name `{orig}` from partially-swapped vendor chunk {target_chunk_name} under non-identifier alias `{exported}`; the re-export rewrite has no live form for that alias and the stripped chunk no longer exports it",
                            );
                        }
                    }
                    ExportSpecifier::Namespace(_) => {
                        bail!(
                            "partial-swap consumer gate: {consumer} re-exports the namespace of partially-swapped vendor chunk {target_chunk_name} (`export * as …`); swapped members [{}] would read as `undefined`",
                            swapped_list(),
                        );
                    }
                    ExportSpecifier::Default(_) => {
                        if symbols.contains_key("default") {
                            bail!(
                                "partial-swap consumer gate: {consumer} re-exports the swapped `default` of partially-swapped vendor chunk {target_chunk_name}",
                            );
                        }
                    }
                }
            }
        }
        ModuleDecl::ExportAll(_) => {
            bail!(
                "partial-swap consumer gate: {consumer} uses `export *` from partially-swapped vendor chunk {target_chunk_name}; swapped names [{}] would silently vanish from the re-exporter's surface",
                swapped_list(),
            );
        }
        _ => {}
    }
    Ok(())
}

fn plan_boundary_renames(
    artifact: &ChunkBundle,
    vendor: &BTreeMap<String, VendorMark>,
    resolved_chunks: &BTreeMap<&str, ResolvedVendorChunk>,
) -> Result<Vec<BoundaryRenamePlan>> {
    let mut plans = Vec::new();
    for (chunk_path, _mark) in vendor.iter().filter(|(_, mark)| {
        matches!(
            mark.level,
            VendorLevel::BoundaryRename | VendorLevel::Swap(_)
        )
    }) {
        let chunk = &resolved_chunks[chunk_path.as_str()];
        let vendor_ast = vendor_entry_ast(artifact, "boundary_rename", chunk)?;
        let mapping = collect_boundary_mapping(&vendor_ast.module);
        validate_boundary_mapping_collisions(&vendor_ast.module, &mapping, chunk_path)?;
        plans.push(BoundaryRenamePlan {
            chunk_id: chunk.chunk_id,
            entry_file: chunk.entry_file.clone(),
            mapping,
        });
    }
    Ok(plans)
}

struct FullSwapJob {
    chunk_path: String,
    chunk_id: ChunkId,
    chunk_name: String,
    entry_file: String,
    package: String,
    version: String,
    subpath: String,
    wrapper_shape: Option<WrapperShape>,
    vendor_exports: BTreeSet<String>,
    /// Chunk export names verified to alias the chunk's own default
    /// export (bound to the same local binding). Consumed by the
    /// `named_from_module_default` wrapper-shape check.
    vendor_default_aliases: BTreeSet<String>,
}

fn plan_full_swaps(
    artifact: &ChunkBundle,
    vendor: &BTreeMap<String, VendorMark>,
    resolved_chunks: &BTreeMap<&str, ResolvedVendorChunk>,
    options: &VendorPlanOptions<'_>,
) -> Result<Vec<FullSwapPlan>> {
    let mut jobs = Vec::new();
    for (chunk_path, mark) in vendor {
        let VendorLevel::Swap(swap) = &mark.level else {
            continue;
        };
        let chunk = &resolved_chunks[chunk_path.as_str()];
        let entry_ast = vendor_entry_ast(artifact, "swap_vendor_chunks", chunk)?;
        let vendor_exports = collect_exported_names(&entry_ast.module);
        let declared_default_aliases: BTreeSet<String> =
            swap.default_export_aliases.iter().cloned().collect();
        let unknown_aliases = set_diff(&declared_default_aliases, &vendor_exports);
        if !unknown_aliases.is_empty() {
            bail!(
                "swap_vendor_chunks vendor entry {} default_export_aliases names exports the chunk does not declare: [{}]",
                chunk_path,
                unknown_aliases.into_iter().collect::<Vec<_>>().join(",")
            );
        }
        // Author-asserted default aliases (see `SwapMark::default_export_aliases`)
        // join the statically verified set so a chunk that re-exports the package
        // default under a minified name without its own `default` export can still
        // pass the `named_from_module_default` soundness check.
        let mut vendor_default_aliases = verified_default_alias_export_names(&entry_ast.module);
        vendor_default_aliases.extend(declared_default_aliases);
        jobs.push(FullSwapJob {
            chunk_path: chunk_path.clone(),
            chunk_id: chunk.chunk_id,
            chunk_name: chunk.chunk_name.clone(),
            entry_file: chunk.entry_file.clone(),
            package: swap.package.clone(),
            version: swap.version.clone(),
            subpath: swap.subpath.clone(),
            wrapper_shape: swap.wrapper_shape,
            vendor_exports,
            vendor_default_aliases,
        });
    }
    // Rayon workers don't inherit `GLOBALS`; re-set per worker so any
    // `Mark::new()` / `Id` use stays in the caller's arena.
    GLOBALS.with(|globals| {
        jobs.into_par_iter()
            .map(|job| GLOBALS.set(globals, || resolve_full_swap(job, options)))
            .collect::<Result<Vec<_>>>()
    })
}

fn resolve_full_swap(job: FullSwapJob, options: &VendorPlanOptions<'_>) -> Result<FullSwapPlan> {
    let installed =
        read_installed_package_metadata(&job.package, options.package_roots, options.packages_root)
            .with_context(|| format!("reading metadata for package {}", job.package))?;
    let installed_version = installed
        .get("version")
        .and_then(Value::as_str)
        .context("package metadata missing version")?;
    if installed_version != job.version {
        bail!(
            "swap_vendor_chunks vendor entry {} version mismatch for {}: spec={}, installed={installed_version}",
            job.chunk_path,
            job.package,
            job.version,
        );
    }
    let upstream_path = resolve_package_subpath(
        &job.package,
        &job.subpath,
        options.package_roots,
        options.packages_root,
    )?;
    let upstream_code = fs::read_to_string(&upstream_path)
        .with_context(|| format!("reading {}", upstream_path.display()))?;

    let wrapper_source = match job.wrapper_shape {
        Some(WrapperShape::NamedFromDefault) => {
            let upstream_ast =
                parse_js_module(&upstream_path.display().to_string(), &upstream_code)?;
            let object_keys =
                collect_default_export_object_keys(&upstream_ast.module, &job.chunk_path)?;
            let non_default_exports = job
                .vendor_exports
                .iter()
                .filter(|name| name.as_str() != "default")
                .cloned()
                .collect::<BTreeSet<_>>();
            let missing = set_diff(&non_default_exports, &object_keys);
            if !missing.is_empty() {
                bail!(
                    "swap_vendor_chunks vendor entry {} named-from-default wrapper shape mismatch for {}@{}: vendor named exports missing from upstream default object keys=[{}]",
                    job.chunk_path,
                    job.package,
                    job.version,
                    missing.into_iter().collect::<Vec<_>>().join(",")
                );
            }
            Some(generate_named_from_default_wrapper(
                &upstream_ast,
                &non_default_exports,
            )?)
        }
        Some(WrapperShape::NamedFromJsonDefault) => {
            let upstream_json = serde_json::from_str::<Value>(&upstream_code).with_context(|| {
                format!(
                    "swap_vendor_chunks vendor entry {} named-from-json-default: upstream JSON parse failed",
                    job.chunk_path
                )
            })?;
            let object = upstream_json
                .as_object()
                .context("named-from-json-default upstream JSON must be an object")?;
            let object_keys = object.keys().cloned().collect::<BTreeSet<_>>();
            let non_default_exports = job
                .vendor_exports
                .iter()
                .filter(|name| name.as_str() != "default")
                .cloned()
                .collect::<BTreeSet<_>>();
            let missing = set_diff(&non_default_exports, &object_keys);
            if !missing.is_empty() {
                bail!(
                    "swap_vendor_chunks vendor entry {} named-from-json-default wrapper shape mismatch for {}@{}: vendor named exports missing from upstream JSON keys=[{}]",
                    job.chunk_path,
                    job.package,
                    job.version,
                    missing.into_iter().collect::<Vec<_>>().join(",")
                );
            }
            Some(generate_named_from_json_default_wrapper(
                &upstream_json,
                &non_default_exports,
            )?)
        }
        Some(WrapperShape::NamedFromModuleDefault) => {
            // The wrapper re-exports the upstream default under every
            // vendor named-export name (`export const <name> =
            // <default>;`). That equation only holds when the chunk
            // itself binds <name> to the same local as its default
            // export; anything else would silently alias an unrelated
            // export to the upstream default.
            let claimed = job
                .vendor_exports
                .iter()
                .filter(|name| name.as_str() != "default")
                .cloned()
                .collect::<BTreeSet<_>>();
            let unverified = set_diff(&claimed, &job.vendor_default_aliases);
            if !unverified.is_empty() {
                bail!(
                    "swap_vendor_chunks vendor entry {} named-from-module-default: vendor named exports [{}] are not verified aliases of the chunk's default export; the wrapper would re-export the upstream default under unrelated names",
                    job.chunk_path,
                    unverified.into_iter().collect::<Vec<_>>().join(","),
                );
            }
            let upstream_ast =
                parse_js_module(&upstream_path.display().to_string(), &upstream_code)?;
            Some(generate_named_from_module_default_wrapper(
                &upstream_ast,
                &job.vendor_exports,
                &job.chunk_path,
            )?)
        }
        None => {
            let upstream_ast =
                parse_js_module(&upstream_path.display().to_string(), &upstream_code)?;
            let upstream_exports = collect_exported_names(&upstream_ast.module);
            let missing = set_diff(&job.vendor_exports, &upstream_exports);
            if !missing.is_empty() {
                bail!(
                    "swap_vendor_chunks vendor entry {} export shape mismatch for {}@{}: vendor exports not found upstream=[{}]",
                    job.chunk_path,
                    job.package,
                    job.version,
                    missing.into_iter().collect::<Vec<_>>().join(",")
                );
            }
            None
        }
    };

    let wrapper = wrapper_source
        .zip(options.output_wrapper_dir)
        .map(|(source, wrapper_dir)| PlannedWrapper {
            abs_path: wrapper_output_path(wrapper_dir, &job.chunk_name, &job.entry_file),
            source,
        });
    let generated_wrapper_path = wrapper.as_ref().and_then(|wrapper| {
        options
            .output_manifest_path
            .map(|manifest_path| manifest_relative_path(manifest_path, &wrapper.abs_path))
    });
    Ok(FullSwapPlan {
        chunk_id: job.chunk_id,
        vendor_exports: job.vendor_exports,
        wrapper,
        resolution: VendorResolution {
            chunk_id: job.chunk_name,
            chunk_path: job.chunk_path,
            entry_file: job.entry_file,
            package: job.package,
            version: job.version,
            subpath: job.subpath,
            wrapper_shape: job.wrapper_shape,
            generated_wrapper_path,
        },
    })
}

fn plan_partial_swaps(
    artifact: &ChunkBundle,
    vendor: &BTreeMap<String, VendorMark>,
    resolved_chunks: &BTreeMap<&str, ResolvedVendorChunk>,
    options: &VendorPlanOptions<'_>,
) -> Result<BTreeMap<ChunkId, ChunkPartialSwapPlan>> {
    let mut plans = BTreeMap::new();
    for (chunk_path, mark) in vendor {
        let VendorLevel::PartialSwap(partial) = &mark.level else {
            continue;
        };
        let chunk = &resolved_chunks[chunk_path.as_str()];
        let entry_ast = vendor_entry_ast(artifact, "apply_partial_vendor_swaps", chunk)?;
        // `default` is a swappable name when the chunk binds it via the
        // named form (`export { x as default }`), which the strip pass
        // can map to a chunk-local binding.
        let chunk_exports = collect_exported_names(&entry_ast.module);

        validate_partial_swap_symbols::<PartialSwapPackage>(
            "apply_partial_vendor_swaps",
            chunk_path,
            &chunk.chunk_name,
            &chunk_exports,
            &partial.symbols,
            None,
        )?;

        // Per-package validation: installed version + upstream subpath
        // exists + every declared upstream_export is actually a named
        // export of the upstream subpath.
        let resolve_options = ResolvePartialSwapPackageOptions {
            stage: "apply_partial_vendor_swaps",
            chunk_path,
            package_roots: options.package_roots,
            packages_root: options.packages_root,
        };
        let mut package_resolutions: BTreeMap<String, PartialSwapPackageResolution> =
            BTreeMap::new();
        for (package_name, package) in &partial.packages {
            let any_member_for_package = partial
                .symbols
                .values()
                .any(|s| s.package == *package_name && matches!(s.kind, PartialSwapKind::Member));
            let upstream_path = resolve_partial_swap_package(
                &resolve_options,
                package_name,
                PartialSwapPackageCoords::from(package),
                any_member_for_package.then_some("kind=member"),
            )?;
            let upstream_code = fs::read_to_string(&upstream_path)
                .with_context(|| format!("reading {}", upstream_path.display()))?;
            let upstream_ast =
                parse_js_module(&upstream_path.display().to_string(), &upstream_code)?;
            let upstream_exports = collect_exported_names(&upstream_ast.module);
            // Packages that re-export everything from a sibling module
            // (`export * from "./other.js"`) can't be fully enumerated
            // by a single-file `collect_exported_names`. Skip the strict
            // name check in that case — the caller's spec is responsible
            // for naming a real upstream symbol. (zod's `index.js` is the
            // canonical example: `export * from "./v4/classic/external.js"`
            // is the only way the named schemas like `object`, `array`
            // become visible.)
            let upstream_has_export_star = module_has_export_star(&upstream_ast.module);
            for (chunk_export, symbol) in &partial.symbols {
                if symbol.package != *package_name {
                    continue;
                }
                // Only kind=member symbols cite an upstream named
                // export; kind=namespace/default replace the whole
                // import with a namespace/default reference, no
                // member-name lookup needed.
                let Some(upstream_export) = symbol.upstream_export.as_deref() else {
                    continue;
                };
                if upstream_has_export_star {
                    continue;
                }
                if !upstream_exports.contains(upstream_export) {
                    bail!(
                        "apply_partial_vendor_swaps vendor entry {chunk_path}: symbol `{chunk_export}` targets {package_name}#{upstream_export} but upstream does not export it (known: [{}])",
                        upstream_exports
                            .iter()
                            .cloned()
                            .collect::<Vec<_>>()
                            .join(",")
                    );
                }
            }
            package_resolutions.insert(
                package_name.clone(),
                PartialSwapPackageResolution {
                    namespace: package.namespace.clone(),
                    version: package.version.clone(),
                    subpath: package.subpath.clone(),
                },
            );
        }

        plans.insert(
            chunk.chunk_id,
            ChunkPartialSwapPlan {
                chunk_path: chunk_path.clone(),
                entry_file: chunk.entry_file.clone(),
                packages: partial.packages.clone(),
                symbols: partial.symbols.clone(),
                resolution: ChunkPartialSwapResolution {
                    chunk_id: chunk.chunk_name.clone(),
                    chunk_path: chunk_path.clone(),
                    packages: package_resolutions,
                    symbols: build_partial_swap_symbol_resolutions(&partial.symbols),
                },
            },
        );
    }
    Ok(plans)
}

fn plan_bundled_partial_swaps(
    artifact: &ChunkBundle,
    vendor: &BTreeMap<String, VendorMark>,
    resolved_chunks: &BTreeMap<&str, ResolvedVendorChunk>,
    options: &VendorPlanOptions<'_>,
) -> Result<BTreeMap<ChunkId, ChunkBundledPartialSwapPlan>> {
    let mut plans = BTreeMap::new();
    for (chunk_path, mark) in vendor {
        let VendorLevel::BundledPartialSwap(bundled) = &mark.level else {
            continue;
        };
        let chunk = &resolved_chunks[chunk_path.as_str()];
        let entry_ast = vendor_entry_ast(artifact, "apply_bundled_partial_vendor_swaps", chunk)?;
        let chunk_exports = collect_exported_names(&entry_ast.module);

        validate_partial_swap_symbols(
            "apply_bundled_partial_vendor_swaps",
            chunk_path,
            &chunk.chunk_name,
            &chunk_exports,
            &bundled.symbols,
            Some(&bundled.packages),
        )?;

        let bundle_code = fs::read_to_string(&bundled.bundle.path)
            .with_context(|| format!("reading {}", bundled.bundle.path.display()))?;
        let bundle_ast = parse_js_module(&bundled.bundle.path.display().to_string(), &bundle_code)?;
        let bundle_exports = collect_exported_names(&bundle_ast.module);
        for (package_name, package) in &bundled.packages {
            if package.bundle_export != "default" && !is_valid_identifier(&package.bundle_export) {
                bail!(
                    "apply_bundled_partial_vendor_swaps vendor entry {chunk_path}: package `{package_name}` bundle_export `{}` is not a valid JS identifier",
                    package.bundle_export
                );
            }
            if !bundle_exports.contains(&package.bundle_export) {
                bail!(
                    "apply_bundled_partial_vendor_swaps vendor entry {chunk_path}: package `{package_name}` targets bundle export `{}` but bundle exports only [{}]",
                    package.bundle_export,
                    bundle_exports.iter().cloned().collect::<Vec<_>>().join(",")
                );
            }
        }

        let assets = plan_bundled_partial_swap_assets(
            options.output_wrapper_dir,
            &chunk.chunk_name,
            &bundled.bundle.path,
            bundle_code,
            &bundled.packages,
        )?;

        let generated_bundle_path = options
            .output_manifest_path
            .map(|manifest_path| manifest_relative_path(manifest_path, &assets.bundle_abs_path));
        let bundle_resolution = BundledPartialSwapBundleResolution {
            source_path: bundled.bundle.path.display().to_string(),
            generated_bundle_path,
        };

        let mut package_resolutions: BTreeMap<String, BundledPartialSwapPackageResolution> =
            BTreeMap::new();
        let mut package_targets: BTreeMap<String, BundledPartialSwapPackageTarget> =
            BTreeMap::new();
        let resolve_options = ResolvePartialSwapPackageOptions {
            stage: "apply_bundled_partial_vendor_swaps",
            chunk_path,
            package_roots: options.package_roots,
            packages_root: options.packages_root,
        };
        for (package_name, package) in &bundled.packages {
            let any_member_like_for_package = bundled.symbols.values().any(|s| {
                s.package == *package_name
                    && matches!(s.kind, PartialSwapKind::Member | PartialSwapKind::Named)
            });
            resolve_partial_swap_package(
                &resolve_options,
                package_name,
                PartialSwapPackageCoords::from(package),
                any_member_like_for_package.then_some("kind=member/named"),
            )?;
            let facade = assets.facades.get(package_name).with_context(|| {
                format!(
                    "apply_bundled_partial_vendor_swaps generated no facade for package `{package_name}`"
                )
            })?;
            let generated_facade_path = options
                .output_manifest_path
                .map(|manifest_path| manifest_relative_path(manifest_path, &facade.abs_path));
            package_resolutions.insert(
                package_name.clone(),
                BundledPartialSwapPackageResolution {
                    namespace: package.namespace.clone(),
                    version: package.version.clone(),
                    subpath: package.subpath.clone(),
                    bundle_export: package.bundle_export.clone(),
                    generated_facade_path,
                },
            );
            package_targets.insert(
                package_name.clone(),
                BundledPartialSwapPackageTarget {
                    namespace: package.namespace.clone(),
                    facade_app_path: facade.app_path.clone(),
                },
            );
        }

        plans.insert(
            chunk.chunk_id,
            ChunkBundledPartialSwapPlan {
                chunk_path: chunk_path.clone(),
                entry_file: chunk.entry_file.clone(),
                packages: package_targets,
                symbols: bundled.symbols.clone(),
                assets,
                resolution: ChunkBundledPartialSwapResolution {
                    chunk_id: chunk.chunk_name.clone(),
                    chunk_path: chunk_path.clone(),
                    bundle: bundle_resolution,
                    packages: package_resolutions,
                    symbols: build_partial_swap_symbol_resolutions(&bundled.symbols),
                },
            },
        );
    }
    Ok(plans)
}
