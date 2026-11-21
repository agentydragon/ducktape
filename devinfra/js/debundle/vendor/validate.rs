//! Shared vendor-plan validation and pre/post-rewrite consumer checks.
//!
//! The partial and bundled-partial plan phases perform near-identical
//! validation/resolution of their vendor marks; the shared pieces live
//! here so the phases shrink to mode-specific glue while diagnostics retain
//! their phase context.

use source_identity::ChunkId;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::PathBuf;

use anyhow::{Context, Result, bail};
use serde_json::Value;

use artifact::{ArtifactIndexes, ChunkBundle, list_chunk_file_paths};
use binding_targets::{imported_export_name, module_export_name};
use js_ast::{ParsedJsModule, is_binding_identifier};
use spec::{BundledPartialSwapPackage, PartialSwapKind, PartialSwapPackage, PartialSwapSymbol};
use swc_ecma_ast::{
    ExportSpecifier, Id, ImportSpecifier, ModuleDecl, ModuleExportName, ModuleItem,
};

use crate::manifests::PartialSwapSymbolResolution;
use crate::output_imports::{MaterializedOutputChunkIndex, resolve_partial_swap_import_target};
use crate::packages::{read_installed_package_metadata, resolve_package_subpath};
use crate::plan::VendorResolutionPlan;

/// Parse the vendor map key `<chunk_name>.js` into the chunk name.
pub(crate) fn vendor_chunk_name(chunk_path: &str, stage: &str) -> Result<String> {
    if chunk_path.is_empty() {
        bail!("{stage}: empty chunk path");
    }
    let Some(chunk_name) = chunk_path.strip_suffix(".js") else {
        bail!("{stage}: chunk path must end in .js: {chunk_path}");
    };
    Ok(chunk_name.to_string())
}

/// A vendor-marked chunk resolved against the artifact: the typed id,
/// the display name diagnostics use, and the chunk's entry file.
pub(crate) struct ResolvedVendorChunk {
    pub chunk_id: ChunkId,
    pub chunk_name: String,
    pub entry_file: String,
}

pub(crate) fn vendor_entry_ast<'a>(
    artifact: &'a ChunkBundle,
    stage: &str,
    chunk: &ResolvedVendorChunk,
) -> Result<&'a ParsedJsModule> {
    artifact
        .js_chunk(chunk.chunk_id)?
        .get_file(&chunk.entry_file)
        .and_then(|file| file.ast())
        .with_context(|| {
            format!(
                "{stage} vendor chunk {} is missing entry AST",
                chunk.chunk_name
            )
        })
}

/// Validate the per-symbol half of a partial-swap mark: every declared
/// chunk_export must exist on the chunk's export surface (unless
/// `local` overrides the lookup), `kind` and `upstream_export` must
/// agree, and `local` must be a valid identifier. `packages` is `Some`
/// for the bundled dispatcher, which additionally rejects symbols
/// referencing unknown packages.
pub(crate) fn validate_partial_swap_symbols<P>(
    stage: &str,
    chunk_path: &str,
    chunk_name: &str,
    chunk_exports: &BTreeSet<String>,
    symbols: &BTreeMap<String, PartialSwapSymbol>,
    packages: Option<&BTreeMap<String, P>>,
) -> Result<()> {
    // Validate every declared chunk_export exists as an actual export
    // on the chunk; otherwise the per-symbol rewrite would silently
    // miss the binding.
    for (chunk_export, symbol) in symbols {
        if !chunk_exports.contains(chunk_export) && symbol.local.is_none() {
            bail!(
                "{stage} vendor entry {chunk_path}: chunk {chunk_name} does not export `{chunk_export}` (known exports: [{}])",
                chunk_exports.iter().cloned().collect::<Vec<_>>().join(",")
            );
        }
    }

    // Per-symbol shape validation: `kind: member` and `kind: named`
    // require `upstream_export`; `kind: namespace` / `kind: default`
    // forbid it.
    for (chunk_export, symbol) in symbols {
        if let Some(packages) = packages
            && !packages.contains_key(&symbol.package)
        {
            bail!(
                "{stage} vendor entry {chunk_path}: symbol `{chunk_export}` references unknown package `{}`",
                symbol.package
            );
        }
        match (symbol.kind, symbol.upstream_export.as_deref()) {
            (PartialSwapKind::Member | PartialSwapKind::Named, None) => bail!(
                "{stage} vendor entry {chunk_path}: symbol `{chunk_export}` (kind={:?}) missing required `upstream_export`",
                symbol.kind
            ),
            (PartialSwapKind::Namespace | PartialSwapKind::Default, Some(_)) => bail!(
                "{stage} vendor entry {chunk_path}: symbol `{chunk_export}` (kind={:?}) must not set `upstream_export`",
                symbol.kind
            ),
            _ => {}
        }
        validate_optional_local_symbol(symbol.local.as_deref(), stage, chunk_path, chunk_export)?;
    }
    Ok(())
}

fn validate_optional_local_symbol(
    local: Option<&str>,
    stage: &str,
    chunk_path: &str,
    chunk_export: &str,
) -> Result<()> {
    let Some(local) = local else {
        return Ok(());
    };
    if !is_binding_identifier(local) {
        bail!(
            "{stage} vendor entry {chunk_path}: symbol `{chunk_export}` local `{local}` is not a valid JS identifier",
        );
    }
    Ok(())
}

/// Spec-level package coordinates shared by the partial and bundled
/// package-target types.
pub(crate) struct PartialSwapPackageCoords<'a> {
    pub version: &'a str,
    pub subpath: &'a str,
    pub namespace: Option<&'a str>,
}

impl<'a> From<&'a PartialSwapPackage> for PartialSwapPackageCoords<'a> {
    fn from(package: &'a PartialSwapPackage) -> Self {
        Self {
            version: &package.version,
            subpath: &package.subpath,
            namespace: package.namespace.as_deref(),
        }
    }
}

impl<'a> From<&'a BundledPartialSwapPackage> for PartialSwapPackageCoords<'a> {
    fn from(package: &'a BundledPartialSwapPackage) -> Self {
        Self {
            version: &package.version,
            subpath: &package.subpath,
            namespace: package.namespace.as_deref(),
        }
    }
}

pub(crate) struct ResolvePartialSwapPackageOptions<'a> {
    pub stage: &'a str,
    pub chunk_path: &'a str,
    pub package_roots: &'a HashMap<String, PathBuf>,
    pub packages_root: &'a Option<PathBuf>,
}

/// Validate one partial-swap package against the installed tree:
/// `namespace` present and identifier-valid when a symbol kind requires
/// it (`namespace_requirer` names the requiring kinds for the
/// diagnostic), installed version matches the spec, and the upstream
/// subpath resolves. Returns the resolved upstream file path.
pub(crate) fn resolve_partial_swap_package(
    opts: &ResolvePartialSwapPackageOptions<'_>,
    package_name: &str,
    package: PartialSwapPackageCoords<'_>,
    namespace_requirer: Option<&str>,
) -> Result<PathBuf> {
    let ResolvePartialSwapPackageOptions {
        stage,
        chunk_path,
        package_roots,
        packages_root,
    } = *opts;
    if let Some(requirer) = namespace_requirer {
        let namespace = package.namespace.with_context(|| format!(
            "{stage} vendor entry {chunk_path}: package `{package_name}` is referenced by a {requirer} symbol but is missing `namespace`",
        ))?;
        if !is_binding_identifier(namespace) {
            bail!(
                "{stage} vendor entry {chunk_path}: package `{package_name}` namespace `{namespace}` is not a valid JS identifier",
            );
        }
    }
    let installed = read_installed_package_metadata(package_name, package_roots, packages_root)
        .with_context(|| format!("reading metadata for package {package_name}"))?;
    let installed_version = installed
        .get("version")
        .and_then(Value::as_str)
        .context("package metadata missing version")?;
    if installed_version != package.version {
        bail!(
            "{stage} vendor entry {chunk_path} version mismatch for {package_name}: spec={}, installed={installed_version}",
            package.version,
        );
    }
    resolve_package_subpath(package_name, package.subpath, package_roots, packages_root)
}

/// Build the wire-facing symbol resolutions (zero-initialized rewrite
/// counts) from a mark's symbol map.
pub(crate) fn build_partial_swap_symbol_resolutions(
    symbols: &BTreeMap<String, PartialSwapSymbol>,
) -> BTreeMap<String, PartialSwapSymbolResolution> {
    symbols
        .iter()
        .map(|(chunk_export, symbol)| {
            (
                chunk_export.clone(),
                PartialSwapSymbolResolution {
                    package: symbol.package.clone(),
                    kind: symbol.kind,
                    upstream_export: symbol.upstream_export.clone(),
                    local: symbol.local.clone(),
                    references_rewritten: 0,
                },
            )
        })
        .collect()
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum PartialSwapConsumerStage {
    /// A source directive may still have a live rewrite.
    Plan,
    /// A surviving reference to a removed export is invalid.
    Emitted,
}

/// Check either input directives or the final emitted artifact. Both scans
/// resolve targets in their own artifact, including materialized imports.
pub(super) fn check_partial_swap_consumers(
    artifact: &ChunkBundle,
    references: &ArtifactIndexes,
    plan: &VendorResolutionPlan,
    stage: PartialSwapConsumerStage,
) -> Result<()> {
    let swapped_by_chunk: BTreeMap<_, _> = plan
        .partial_swaps
        .iter()
        .map(|(chunk, partial)| (*chunk, (&partial.symbols, false)))
        .chain(
            plan.bundled_partial_swaps
                .iter()
                .map(|(chunk, bundled)| (*chunk, (&bundled.symbols, true))),
        )
        .collect();
    if swapped_by_chunk.is_empty() {
        return Ok(());
    }

    let chunk_table = &artifact.chunk_table;
    let materialized_index = MaterializedOutputChunkIndex::build(chunk_table);
    let full_swap_chunks = plan.full_swap_chunk_ids();
    for chunk_artifact in &artifact.chunks {
        let caller = chunk_artifact.chunk_id;
        if full_swap_chunks.contains(&caller) {
            continue;
        }
        let caller_name = chunk_table.name(caller);
        for file_path in list_chunk_file_paths(&chunk_artifact.js) {
            let Some(ast) = chunk_artifact
                .js
                .get_file(&file_path)
                .and_then(|file| file.ast())
            else {
                continue;
            };
            let consumer = format!("{caller_name}/{file_path}");
            let mut member_read_locals = BTreeMap::new();
            for item in &ast.module.body {
                let ModuleItem::ModuleDecl(decl) = item else {
                    continue;
                };
                let Some(source) = consumer_directive_source(decl) else {
                    continue;
                };
                let Some(target) = resolve_partial_swap_import_target(
                    &source,
                    caller,
                    &file_path,
                    references,
                    chunk_table,
                    &materialized_index,
                ) else {
                    continue;
                };
                let Some(&(symbols, bundled)) = swapped_by_chunk.get(&target) else {
                    continue;
                };
                check_consumer_directive(
                    decl,
                    &consumer,
                    chunk_table.name(target),
                    symbols,
                    bundled,
                    stage,
                    &mut member_read_locals,
                )?;
            }
            if !member_read_locals.is_empty() {
                check_local_exports_have_live_rewrite(
                    &ast.module.body,
                    &member_read_locals,
                    &consumer,
                )?;
            }
        }
    }
    Ok(())
}

/// Whether the body rewrite keeps a binding, or a live re-export, for a
/// local `export { <local> }` of this swapped import: a property of the
/// default-only facade, or a dotted member path, has neither.
fn local_export_has_live_rewrite(symbol: &PartialSwapSymbol, bundled: bool) -> bool {
    match symbol.kind {
        PartialSwapKind::Namespace | PartialSwapKind::Default => true,
        PartialSwapKind::Named => !bundled,
        PartialSwapKind::Member => {
            !bundled
                && symbol
                    .upstream_export
                    .as_deref()
                    .is_some_and(|name| !name.contains('.'))
        }
    }
}

fn check_local_exports_have_live_rewrite(
    body: &[ModuleItem],
    member_read_locals: &BTreeMap<Id, String>,
    consumer: &str,
) -> Result<()> {
    for item in body {
        let ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(export)) = item else {
            continue;
        };
        if export.src.is_some() {
            continue;
        }
        for specifier in &export.specifiers {
            if let ExportSpecifier::Named(named) = specifier
                && let ModuleExportName::Ident(orig) = &named.orig
                && let Some(imported) = member_read_locals.get(&orig.to_id())
            {
                bail!(
                    "partial-swap consumer gate: {consumer} locally exports `{}`, its import of swapped name `{imported}`; the swap reads that symbol as a property of the replacement, which has no live export form",
                    orig.sym,
                );
            }
        }
    }
    Ok(())
}

fn check_consumer_directive(
    decl: &ModuleDecl,
    consumer: &str,
    target_chunk_name: &str,
    symbols: &BTreeMap<String, PartialSwapSymbol>,
    bundled: bool,
    stage: PartialSwapConsumerStage,
    member_read_locals: &mut BTreeMap<Id, String>,
) -> Result<()> {
    let swapped_list = || symbols.keys().cloned().collect::<Vec<_>>().join(",");
    match decl {
        ModuleDecl::Import(import) => {
            for specifier in &import.specifiers {
                match specifier {
                    ImportSpecifier::Named(named) => {
                        let imported = imported_export_name(named);
                        let Some(symbol) = symbols.get(&imported) else {
                            continue;
                        };
                        if stage == PartialSwapConsumerStage::Emitted {
                            bail!(
                                "partial-swap consumer gate: {consumer} imports swapped name `{imported}` from partially-swapped vendor chunk {target_chunk_name}; the rewrite did not cover this consumer and the stripped chunk no longer exports it"
                            );
                        }
                        if !local_export_has_live_rewrite(symbol, bundled) {
                            member_read_locals.insert(named.local.to_id(), imported);
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
                        if let Some(symbol) = symbols.get(&orig)
                            && (stage == PartialSwapConsumerStage::Emitted
                                || bundled
                                || symbol.kind == PartialSwapKind::Member)
                        {
                            bail!(
                                "partial-swap consumer gate: {consumer} re-exports swapped name `{orig}` from partially-swapped vendor chunk {target_chunk_name}; this re-export shape has no live rewrite (kind=member symbols and bundled swaps cannot be expressed as re-exports) and the stripped chunk no longer exports it"
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

/// Source of an import or re-export; local exports are not consumers.
fn consumer_directive_source(decl: &ModuleDecl) -> Option<String> {
    let source = match decl {
        ModuleDecl::Import(import) => &import.src,
        ModuleDecl::ExportNamed(named) => named.src.as_ref()?,
        ModuleDecl::ExportAll(export_all) => &export_all.src,
        _ => return None,
    };
    Some(js_ast::str_value(source))
}
