use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use rayon::prelude::*;
mod manifests;
mod plan;
mod strip;
mod validate;
mod wrappers;

use serde_json::Value;
use swc_common::{DUMMY_SP, GLOBALS};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitMut, VisitMutWith, VisitWith};

use analysis::local_namespace_iife_target;
use artifact::{
    ArtifactIndexes, ChunkBundle, ChunkId, ChunkTable, JsFile, JsFileAstParts, join_module_path,
    list_chunk_file_paths, module_path_dirname, normalize_module_path, relative_module_specifier,
};
use binding_targets::{declaration_ids, declaration_name_strings, module_export_name};
use js_ast::{ParsedJsModule, str_value};
#[cfg(test)]
use js_ast::{emit_js_module, parse_js_module};
pub use manifests::*;
use plan::{ChunkBundledPartialSwapPlan, ChunkPartialSwapPlan};
pub use plan::{VendorPlanOptions, VendorResolutionPlan, build_vendor_resolution_plan};
use spec::PartialSwapKind;
pub use strip::{
    ChunkStripStats, StripSwappedVendorExportsOptions, strip_swapped_vendor_exports_with_options,
};
use wrappers::{write_planned_bundled_assets, write_planned_wrapper};

pub fn rename_vendor_exports(
    mut artifact: ChunkBundle,
    plan: &VendorResolutionPlan,
    references: &ArtifactIndexes,
) -> Result<RenameVendorExportsResult> {
    let mut mappings = VendorExportMappings::new();
    let mut total_rewrites = 0usize;
    let mut chunks_with_mapping = 0usize;
    let mut details = Vec::new();
    let chunk_table = artifact.chunk_table.clone();

    for op in &plan.boundary_renames {
        if !op.mapping.is_empty() {
            chunks_with_mapping += 1;
            mappings
                .entry(op.chunk_id)
                .or_default()
                .insert(op.entry_file.clone(), op.mapping.clone());
        }
    }

    let mut caller_counts_by_target = BTreeMap::<(ChunkId, String), BTreeMap<String, usize>>::new();
    if !mappings.is_empty() {
        let mut jobs = Vec::new();
        for (caller_chunk_index, chunk_artifact) in artifact.chunks.iter_mut().enumerate() {
            let caller_chunk_id = chunk_artifact.chunk_id;
            let caller_chunk_name = chunk_table.name(caller_chunk_id).to_string();
            for file_path in list_chunk_file_paths(&chunk_artifact.js) {
                let has_ast = chunk_artifact
                    .js
                    .get_file(&file_path)
                    .and_then(|file| file.ast())
                    .is_some();
                if !has_ast {
                    continue;
                }
                let (parts, ast) = chunk_artifact
                    .js
                    .remove_file(&file_path)
                    .and_then(|file| file.into_ast_parts())
                    .with_context(|| format!("missing AST for {caller_chunk_name}/{file_path}"))?;
                jobs.push(VendorRenameFileJob {
                    caller_chunk_index,
                    caller_chunk_id,
                    caller_chunk_name: caller_chunk_name.clone(),
                    file_path,
                    parts,
                    ast,
                });
            }
        }
        let chunk_table_ref = &chunk_table;
        // Rayon workers don't inherit `GLOBALS`; re-set per worker so any
        // `Mark::new()` / `Id` use stays in the caller's arena.
        let results = GLOBALS.with(|globals| {
            jobs.into_par_iter()
                .map(|job| {
                    GLOBALS.set(globals, || {
                        rename_vendor_imports_in_file(job, references, &mappings, chunk_table_ref)
                    })
                })
                .collect::<Vec<_>>()
        });
        for result in results {
            artifact
                .chunks
                .get_mut(result.caller_chunk_index)
                .with_context(|| format!("missing chunk index {}", result.caller_chunk_index))?
                .js
                .insert_file(JsFile::from_ast_parts(result.parts, result.ast));
            for (target, rewrites) in result.rewrites_by_target {
                total_rewrites += rewrites;
                caller_counts_by_target.entry(target).or_default().insert(
                    format!("{}/{}", result.caller_chunk_name, result.file_path),
                    rewrites,
                );
            }
        }
    }

    let considered = plan.boundary_renames.len();
    for op in &plan.boundary_renames {
        let caller_counts = caller_counts_by_target
            .remove(&(op.chunk_id, op.entry_file.clone()))
            .unwrap_or_default();
        let chunk_rewrites = caller_counts.values().sum();
        details.push(RenameVendorExportsDetail {
            chunk_path: op.chunk_path.clone(),
            chunk_id: chunk_table.name(op.chunk_id).to_string(),
            mapping_size: op.mapping.len(),
            rewrites: chunk_rewrites,
            callers: caller_counts
                .into_iter()
                .map(|(file, rewrites)| RenameVendorExportsCaller { file, rewrites })
                .collect(),
        });
    }

    Ok(RenameVendorExportsResult {
        artifact,
        manifest: RenameVendorExportsManifest {
            counts: RenameVendorExportsCounts {
                considered,
                chunks_with_mapping,
                rewrites: total_rewrites,
            },
            details,
        },
    })
}

/// chunk → entry file → vendor-local name → public export name.
type VendorExportMappings = BTreeMap<ChunkId, BTreeMap<String, BTreeMap<String, String>>>;

struct VendorRenameFileJob {
    caller_chunk_index: usize,
    caller_chunk_id: ChunkId,
    caller_chunk_name: String,
    file_path: String,
    parts: JsFileAstParts,
    ast: ParsedJsModule,
}

struct VendorRenameFileResult {
    caller_chunk_index: usize,
    caller_chunk_name: String,
    file_path: String,
    parts: JsFileAstParts,
    ast: ParsedJsModule,
    rewrites_by_target: BTreeMap<(ChunkId, String), usize>,
}

fn rename_vendor_imports_in_file(
    mut job: VendorRenameFileJob,
    references: &ArtifactIndexes,
    mappings: &VendorExportMappings,
    chunk_table: &ChunkTable,
) -> VendorRenameFileResult {
    let mut rewriter = VendorImportRenamer {
        references,
        chunk_table,
        caller_chunk_id: job.caller_chunk_id,
        caller_file: job.file_path.clone(),
        mappings,
        rewrites_by_target: BTreeMap::new(),
    };
    job.ast.module.visit_mut_with(&mut rewriter);
    VendorRenameFileResult {
        caller_chunk_index: job.caller_chunk_index,
        caller_chunk_name: job.caller_chunk_name,
        file_path: job.file_path,
        parts: job.parts,
        ast: job.ast,
        rewrites_by_target: rewriter.rewrites_by_target,
    }
}

/// Apply the plan's full swaps: verify caller import alignment against
/// the current (post-rename) indexes, write planned wrappers, and
/// remove the swapped chunks from the artifact. All resolution
/// decisions (version checks, wrapper-shape validation, wrapper
/// generation) were made at plan time.
pub fn swap_vendor_chunks(
    mut artifact: ChunkBundle,
    plan: &VendorResolutionPlan,
    references: &ArtifactIndexes,
    write: bool,
) -> Result<SwapVendorChunksResult> {
    let chunk_table = artifact.chunk_table.clone();
    let swap_chunk_ids: BTreeSet<ChunkId> =
        plan.full_swaps.iter().map(|swap| swap.chunk_id).collect();
    let import_alignment_index = build_import_alignment_index(references, &swap_chunk_ids);
    let mut removed_chunk_ids = BTreeSet::new();
    let mut resolutions: BTreeMap<String, VendorResolution> = BTreeMap::new();
    for swap in &plan.full_swaps {
        for record in import_alignment_index
            .get(&swap.chunk_id)
            .into_iter()
            .flatten()
        {
            for imported_name in &record.named_imports {
                if swap.vendor_exports.contains(imported_name) {
                    continue;
                }
                bail!(
                    "swap_vendor_chunks vendor entry {} import alignment failed: caller={}/{} imports unknown specifier \"{}\" from vendor {} (known: [{}])",
                    swap.resolution.chunk_path,
                    chunk_table.name(record.caller_chunk_id),
                    record.caller_file,
                    imported_name,
                    swap.resolution.chunk_id,
                    swap.vendor_exports
                        .iter()
                        .cloned()
                        .collect::<Vec<_>>()
                        .join(",")
                );
            }
        }
        if write && let Some(wrapper) = &swap.wrapper {
            write_planned_wrapper(&wrapper.abs_path, &wrapper.source)?;
        }
        artifact.remove_chunk(swap.chunk_id);
        removed_chunk_ids.insert(swap.resolution.chunk_id.clone());
        resolutions.insert(swap.resolution.chunk_path.clone(), swap.resolution.clone());
    }

    let swapped = resolutions.len();
    Ok(SwapVendorChunksResult {
        artifact,
        manifest: VendorResolutionManifest {
            resolutions,
            counts: VendorResolutionCounts { swapped },
        },
        removed_chunk_ids,
    })
}

/// Bail when a boundary-mapping key (a vendor-LOCAL binding name) is
/// itself a genuine export name of the vendor entry bound to a
/// *different* local. The caller-side rewrite treats `import { k }` as
/// "the caller spelled export `<mapping[k]>` by its vendor-local name" —
/// but when the vendor really exports the name `k` (from another local),
/// that import is legitimate and rewriting it would silently rebind the
/// caller to the wrong value.
fn validate_boundary_mapping_collisions(
    module: &Module,
    mapping: &BTreeMap<String, String>,
    chunk_path: &str,
) -> Result<()> {
    if mapping.is_empty() {
        return Ok(());
    }
    // export name -> Some(local sym) when the export aliases a plain
    // local binding; None when its local identity is not a local ident
    // (forwarded `export … from`, string-literal orig).
    let mut export_locals: BTreeMap<String, Option<String>> = BTreeMap::new();
    for item in &module.body {
        match item {
            ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) => {
                for specifier in &named.specifiers {
                    let ExportSpecifier::Named(named_spec) = specifier else {
                        continue;
                    };
                    let exported = named_spec
                        .exported
                        .as_ref()
                        .map(module_export_name)
                        .unwrap_or_else(|| module_export_name(&named_spec.orig));
                    let local = match (&named.src, &named_spec.orig) {
                        (None, ModuleExportName::Ident(local)) => Some(local.sym.to_string()),
                        _ => None,
                    };
                    export_locals.insert(exported, local);
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
                for name in declaration_name_strings(&export_decl.decl) {
                    export_locals.insert(name.clone(), Some(name));
                }
            }
            _ => {}
        }
    }
    for local_name in mapping.keys() {
        let Some(identity) = export_locals.get(local_name) else {
            continue;
        };
        if identity.as_deref() != Some(local_name.as_str()) {
            let bound_to = identity
                .as_deref()
                .map(|local| format!("local `{local}`"))
                .unwrap_or_else(|| "a non-local origin".to_string());
            bail!(
                "rename_vendor_exports vendor entry {chunk_path}: boundary mapping key `{local_name}` collides with a genuine export named `{local_name}` bound to {bound_to}; rewriting caller imports of `{local_name}` would silently rebind them to the wrong value",
            );
        }
    }
    Ok(())
}

fn collect_boundary_mapping(module: &Module) -> BTreeMap<String, String> {
    let mut mapping = BTreeMap::new();
    for item in &module.body {
        let ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) = item else {
            continue;
        };
        if named.src.is_some() || named.specifiers.is_empty() {
            continue;
        }
        for specifier in &named.specifiers {
            let ExportSpecifier::Named(named_specifier) = specifier else {
                continue;
            };
            let ModuleExportName::Ident(local) = &named_specifier.orig else {
                continue;
            };
            let exported_name = named_specifier
                .exported
                .as_ref()
                .map(module_export_name)
                .unwrap_or_else(|| local.sym.to_string());
            if exported_name == local.sym.as_ref() || !is_valid_identifier(&exported_name) {
                continue;
            }
            mapping.insert(local.sym.to_string(), exported_name);
        }
    }
    mapping
}

struct VendorImportRenamer<'a> {
    references: &'a ArtifactIndexes,
    chunk_table: &'a ChunkTable,
    caller_chunk_id: ChunkId,
    caller_file: String,
    mappings: &'a VendorExportMappings,
    rewrites_by_target: BTreeMap<(ChunkId, String), usize>,
}

impl VisitMut for VendorImportRenamer<'_> {
    fn visit_mut_import_decl(&mut self, node: &mut ImportDecl) {
        let source = str_value(&node.src);
        let resolved = self.references.resolve_runtime_import_reference(
            &source,
            self.caller_chunk_id,
            &self.caller_file,
            self.chunk_table,
        );
        let Some(resolved) = resolved else {
            return;
        };
        let target_chunk_id = resolved.target_chunk_id;
        let target_file = resolved.target_file;
        if target_chunk_id == self.caller_chunk_id {
            return;
        }
        let Some(files) = self.mappings.get(&target_chunk_id) else {
            return;
        };
        let Some(mapping) = files.get(&target_file) else {
            return;
        };
        for specifier in &mut node.specifiers {
            let ImportSpecifier::Named(named) = specifier else {
                continue;
            };
            let imported_name = named
                .imported
                .as_ref()
                .map(module_export_name)
                .unwrap_or_else(|| named.local.sym.to_string());
            let Some(mapped) = mapping.get(&imported_name) else {
                continue;
            };
            if mapped == &imported_name {
                continue;
            }
            named.imported = Some(ModuleExportName::Ident(Ident::new_no_ctxt(
                mapped.clone().into(),
                DUMMY_SP,
            )));
            *self
                .rewrites_by_target
                .entry((target_chunk_id, target_file.clone()))
                .or_insert(0) += 1;
        }
    }
}

#[derive(Debug, Clone)]
struct ImportAlignmentRecord {
    caller_chunk_id: ChunkId,
    caller_file: String,
    named_imports: Vec<String>,
}

fn build_import_alignment_index(
    references: &ArtifactIndexes,
    target_chunk_ids: &BTreeSet<ChunkId>,
) -> BTreeMap<ChunkId, Vec<ImportAlignmentRecord>> {
    let mut index = BTreeMap::<ChunkId, Vec<ImportAlignmentRecord>>::new();
    for target_chunk_id in target_chunk_ids {
        for import in references.manifest_imports_targeting_chunk(*target_chunk_id) {
            if import.named_imports.is_empty() {
                continue;
            }
            index
                .entry(*target_chunk_id)
                .or_default()
                .push(ImportAlignmentRecord {
                    caller_chunk_id: import.caller_chunk_id,
                    caller_file: import.caller_file.clone(),
                    named_imports: import.named_imports.clone(),
                });
        }
    }
    index
}

fn read_installed_package_metadata(
    package_name: &str,
    package_roots: &std::collections::HashMap<String, PathBuf>,
    packages_root: &Option<PathBuf>,
) -> Result<Value> {
    let package_root = resolve_package_root(package_name, package_roots, packages_root)?;
    let metadata_path = package_root.join("package.json");
    if !metadata_path.exists() {
        bail!(
            "Package metadata missing for {package_name}: {}",
            metadata_path.display()
        );
    }
    Ok(serde_json::from_str(&fs::read_to_string(metadata_path)?)?)
}

fn resolve_package_root(
    package_name: &str,
    package_roots: &std::collections::HashMap<String, PathBuf>,
    packages_root: &Option<PathBuf>,
) -> Result<PathBuf> {
    if let Some(mapped) = package_roots.get(package_name) {
        let root = absolutize(mapped)?;
        if !root.exists() {
            bail!(
                "Package root not found for {package_name}: {}",
                root.display()
            );
        }
        return Ok(root);
    }
    if !package_roots.is_empty() && packages_root.is_none() {
        bail!("Package root not provided for {package_name}");
    }
    let packages_root = packages_root
        .as_ref()
        .map(|path| absolutize(path.as_path()))
        .transpose()?
        .or_else(default_packages_root)
        .context("Could not locate Bazel-provided package tree in runfiles; pass packagesRoot explicitly for tests/fixtures")?;
    let mut package_root = packages_root.clone();
    for segment in package_path_segments(package_name)? {
        package_root.push(segment);
    }
    assert_path_within_root(
        &package_root,
        &packages_root,
        &format!("Package {package_name} escapes packages root"),
    )?;
    if !package_root.exists() {
        bail!(
            "Package root not found for {package_name}: {}",
            package_root.display()
        );
    }
    Ok(package_root)
}

fn resolve_package_subpath(
    package_name: &str,
    subpath: &str,
    package_roots: &std::collections::HashMap<String, PathBuf>,
    packages_root: &Option<PathBuf>,
) -> Result<PathBuf> {
    let package_root = resolve_package_root(package_name, package_roots, packages_root)?;
    let file_path = absolutize(&package_root.join(subpath))?;
    assert_path_within_root(
        &file_path,
        &package_root,
        &format!("Package {package_name} subpath escapes package root: {subpath}"),
    )?;
    if !file_path.exists() {
        bail!(
            "Package file not found for {package_name}: {subpath} -> {}",
            file_path.display()
        );
    }
    let real_path = file_path.canonicalize()?;
    let real_root = package_root.canonicalize()?;
    assert_path_within_root(
        &real_path,
        &real_root,
        &format!("Package {package_name} subpath realpath escapes package root: {subpath}"),
    )?;
    Ok(file_path)
}

fn default_packages_root() -> Option<PathBuf> {
    for env in ["RUNFILES_DIR", "TEST_SRCDIR"] {
        let Ok(root) = std::env::var(env) else {
            continue;
        };
        let candidate = PathBuf::from(root).join("_main").join("node_modules");
        if candidate.exists() {
            return Some(candidate);
        }
    }
    None
}

fn package_path_segments(package_name: &str) -> Result<Vec<&str>> {
    if package_name.is_empty() {
        bail!("Invalid package name: {package_name}");
    }
    let segments = package_name.split('/').collect::<Vec<_>>();
    if segments
        .iter()
        .any(|segment| segment.is_empty() || *segment == "." || *segment == "..")
    {
        bail!("Invalid package name: {package_name}");
    }
    Ok(segments)
}

fn absolutize(path: &Path) -> Result<PathBuf> {
    if path.is_absolute() {
        Ok(path.to_path_buf())
    } else {
        Ok(std::env::current_dir()?.join(path))
    }
}

fn assert_path_within_root(path: &Path, root: &Path, message: &str) -> Result<()> {
    let rel = path.strip_prefix(root);
    if rel.is_ok() {
        return Ok(());
    }
    bail!("{message}: {}", path.display());
}

fn module_has_export_star(module: &Module) -> bool {
    module.body.iter().any(|item| match item {
        // `export * from "./other.js";`
        ModuleItem::ModuleDecl(ModuleDecl::ExportAll(_)) => true,
        // `export * as ns from "./other.js";` — still re-exports the
        // whole namespace, just under one name.
        ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) => {
            named.src.is_some()
                && named
                    .specifiers
                    .iter()
                    .any(|s| matches!(s, ExportSpecifier::Namespace(_)))
        }
        _ => false,
    })
}

/// Export names of `module`. The `default` name is included whether it
/// comes from an `export default …` declaration or the named form
/// `export { x as default }` — the two spellings are equivalent on the
/// module's export surface.
fn collect_exported_names(module: &Module) -> BTreeSet<String> {
    let mut names = BTreeSet::new();
    for item in &module.body {
        match item {
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(_))
            | ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(_)) => {
                names.insert("default".to_string());
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
                for name in declaration_name_strings(&export_decl.decl) {
                    names.insert(name);
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) => {
                for specifier in &named.specifiers {
                    if let ExportSpecifier::Named(named_specifier) = specifier {
                        names.insert(
                            named_specifier
                                .exported
                                .as_ref()
                                .map(module_export_name)
                                .unwrap_or_else(|| module_export_name(&named_specifier.orig)),
                        );
                    }
                }
            }
            _ => {}
        }
    }
    names
}

/// Export names of `module` that are verified aliases of its default
/// export — bound to the same local binding as the default. Empty when
/// the default's local identity cannot be established.
fn verified_default_alias_export_names(module: &Module) -> BTreeSet<String> {
    let by_export = collect_local_idents_by_export_name(module);
    let default_id = by_export
        .get("default")
        .cloned()
        .or_else(|| default_export_decl_local_id(module));
    let Some(default_id) = default_id else {
        return BTreeSet::new();
    };
    by_export
        .iter()
        .filter(|(name, id)| name.as_str() != "default" && **id == default_id)
        .map(|(name, _)| name.clone())
        .collect()
}

/// Local binding `Id` of the module's `export default …` declaration,
/// when the default is a plain local identifier (or a named fn/class).
fn default_export_decl_local_id(module: &Module) -> Option<Id> {
    for item in &module.body {
        match item {
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(default_expr)) => {
                let Expr::Ident(ident) = &*default_expr.expr else {
                    return None;
                };
                return Some(ident.to_id());
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(default_decl)) => {
                return match &default_decl.decl {
                    DefaultDecl::Fn(function) => function.ident.as_ref().map(Ident::to_id),
                    DefaultDecl::Class(class) => class.ident.as_ref().map(Ident::to_id),
                    DefaultDecl::TsInterfaceDecl(_) => None,
                };
            }
            _ => {}
        }
    }
    None
}

fn collect_default_export_object_keys(
    module: &Module,
    chunk_path: &str,
) -> Result<BTreeSet<String>> {
    for item in &module.body {
        let ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(default_expr)) = item else {
            continue;
        };
        let Expr::Object(object) = &*default_expr.expr else {
            bail!(
                "swap_vendor_chunks vendor entry {chunk_path} named-from-default: upstream default export is not an object literal"
            );
        };
        let mut keys = BTreeSet::new();
        for prop in &object.props {
            let PropOrSpread::Prop(prop) = prop else {
                continue;
            };
            // Two accepted prop shapes — both produce the same wrapper
            // (`export const K = _d.K;`):
            // * `KeyValue` with `Ident` or `Str` key (`{ ping: fn, "pong": fn }`).
            // * `Shorthand` (`{ ping, pong }`) where the local binding name
            //   is the property name; `_d.ping` re-exports the same value
            //   because object-literal shorthand assigns the binding's
            //   value as a data property under the binding's name.
            // Real-world vendor `index.mjs` files use shorthand commonly;
            // both shapes are emit-equivalent for the wrapper's purposes.
            let key = match &**prop {
                Prop::KeyValue(key_value) => prop_name(&key_value.key),
                Prop::Shorthand(ident) => Some(ident.sym.to_string()),
                _ => None,
            };
            if let Some(key) = key {
                keys.insert(key);
            }
        }
        return Ok(keys);
    }
    bail!(
        "swap_vendor_chunks vendor entry {chunk_path} named-from-default: upstream has no export default declaration"
    );
}

fn prop_name(name: &PropName) -> Option<String> {
    match name {
        PropName::Ident(ident) => Some(ident.sym.to_string()),
        PropName::Str(string) => Some(str_value(string)),
        _ => None,
    }
}

// === partial vendor swap =================================================
//
// `swap_vendor_chunks` operates on a *whole* chunk — it wraps the chunk's
// upstream, rewrites caller imports to point at the wrapper, and removes
// the chunk from the artifact. That doesn't work for mixed chunks: Vite
// commonly bundles several packages (zod + @sentry/browser + react +
// lodash + katex + mermaid) into one ESM chunk. Removing it would drop
// every co-bundled package.
//
// `apply_partial_vendor_swaps` is the per-symbol variant: it leaves the
// chunk on disk and rewrites each listed caller-side import specifier
// against an upstream package, then replaces every reference to the
// imported local binding with a namespace member access. The chunk's
// un-swapped exports keep working through the residual import.

trait PartialSwapMappingEntry {
    fn chunk_path(&self) -> &str;
}

impl PartialSwapMappingEntry for ChunkPartialSwapPlan {
    fn chunk_path(&self) -> &str {
        &self.chunk_path
    }
}

impl PartialSwapMappingEntry for ChunkBundledPartialSwapPlan {
    fn chunk_path(&self) -> &str {
        &self.chunk_path
    }
}

struct PartialSwapDispatchOutcome {
    total_references: usize,
    self_rewrite_locals_by_caller: Vec<(ChunkId, BTreeSet<Id>)>,
}

/// Collect AST files from all chunks into parallel jobs, dispatch per-file
/// rewrites via rayon, reassemble results into the artifact, and update
/// per-symbol reference counts in resolutions.
fn dispatch_partial_swap_jobs<M, R, F>(
    artifact: &mut ChunkBundle,
    chunk_table: &ChunkTable,
    mappings: &BTreeMap<ChunkId, M>,
    references: &ArtifactIndexes,
    resolutions: &mut BTreeMap<String, R>,
    rewrite_fn: F,
) -> Result<PartialSwapDispatchOutcome>
where
    M: PartialSwapMappingEntry + Sync,
    R: PartialSwapResolutionSymbols,
    F: Fn(
            PartialSwapFileJob,
            &ArtifactIndexes,
            &BTreeMap<ChunkId, M>,
            &ChunkTable,
            &MaterializedOutputChunkIndex,
        ) -> PartialSwapFileResult
        + Sync,
{
    // Precompute once: the per-import-decl call in `rewrite_swap_import_decls`
    // used to do an O(N_chunks) linear scan over the chunk table to resolve
    // relative `./..` imports against materialized output paths (15.4%
    // inclusive on cpu_atom of `debundle run`). The longest-prefix index
    // collapses that to two HashMap lookups plus a short ancestor walk per
    // import, and also pre-formats the `"<name>.js"` keys so we no longer
    // `format!()` once per import-decl per chunk.
    let materialized_index = MaterializedOutputChunkIndex::build(chunk_table);
    let mut jobs = Vec::new();
    for (caller_chunk_index, chunk_artifact) in artifact.chunks.iter_mut().enumerate() {
        let caller_chunk_id = chunk_artifact.chunk_id;
        let caller_chunk_name = chunk_table.name(caller_chunk_id).to_string();
        for file_path in list_chunk_file_paths(&chunk_artifact.js) {
            let has_ast = chunk_artifact
                .js
                .get_file(&file_path)
                .and_then(|file| file.ast())
                .is_some();
            if !has_ast {
                continue;
            }
            let (parts, ast) = chunk_artifact
                .js
                .remove_file(&file_path)
                .and_then(|file| file.into_ast_parts())
                .with_context(|| format!("missing AST for {caller_chunk_name}/{file_path}"))?;
            jobs.push(PartialSwapFileJob {
                caller_chunk_index,
                caller_chunk_id,
                file_path,
                parts,
                ast,
            });
        }
    }

    let chunk_table_ref = chunk_table;
    let mappings_ref = mappings;
    let materialized_index_ref = &materialized_index;
    let results: Vec<PartialSwapFileResult> = GLOBALS.with(|globals| {
        jobs.into_par_iter()
            .map(|job| {
                GLOBALS.set(globals, || {
                    rewrite_fn(
                        job,
                        references,
                        mappings_ref,
                        chunk_table_ref,
                        materialized_index_ref,
                    )
                })
            })
            .collect()
    });

    let mut total_references = 0usize;
    let mut self_rewrite_import_locals = Vec::new();
    for result in results {
        for ((chunk_id, chunk_export), count) in &result.references_by_symbol {
            let Some(mapping) = mappings_ref.get(chunk_id) else {
                bail!(
                    "partial-swap dispatch: file rewrite reported references to `{chunk_export}` on chunk {}, which has no partial-swap mapping",
                    chunk_table.name(*chunk_id)
                );
            };
            if let Some(resolution) = resolutions.get_mut(mapping.chunk_path())
                && let Some(symbol_resolution) = resolution.symbols_mut().get_mut(chunk_export)
            {
                symbol_resolution.references_rewritten += count;
            }
            total_references += count;
        }
        if !result.self_rewrite_import_locals.is_empty() {
            self_rewrite_import_locals.push((
                result.caller_chunk_id,
                result.self_rewrite_import_locals.clone(),
            ));
        }
        artifact
            .chunks
            .get_mut(result.caller_chunk_index)
            .with_context(|| format!("missing chunk index {}", result.caller_chunk_index))?
            .js
            .insert_file(JsFile::from_ast_parts(result.parts, result.ast));
    }

    Ok(PartialSwapDispatchOutcome {
        total_references,
        self_rewrite_locals_by_caller: self_rewrite_import_locals,
    })
}

/// Apply the plan's partial swaps: rewrite consumer imports /
/// references against the upstream packages. Validation and resolution
/// happened at plan time; the manifest is the plan's resolution
/// projection plus application-time rewrite counts.
pub fn apply_partial_vendor_swaps(
    mut artifact: ChunkBundle,
    plan: &VendorResolutionPlan,
    references: &ArtifactIndexes,
) -> Result<ApplyPartialVendorSwapsResult> {
    let chunk_table = artifact.chunk_table.clone();
    let mut resolutions: BTreeMap<String, ChunkPartialSwapResolution> = plan
        .partial_swaps
        .values()
        .map(|partial| (partial.chunk_path.clone(), partial.resolution.clone()))
        .collect();

    if plan.partial_swaps.is_empty() {
        return Ok(ApplyPartialVendorSwapsResult {
            artifact,
            manifest: ResolutionManifest {
                resolutions,
                counts: PartialSwapResolutionCounts {
                    chunks: 0,
                    symbols: 0,
                    references_rewritten: 0,
                },
            },
        });
    }

    let outcome = dispatch_partial_swap_jobs(
        &mut artifact,
        &chunk_table,
        &plan.partial_swaps,
        references,
        &mut resolutions,
        rewrite_partial_swap_in_file,
    )?;

    let total_symbols: usize = resolutions
        .values()
        .map(|resolution| resolution.symbols.len())
        .sum();
    Ok(ApplyPartialVendorSwapsResult {
        artifact,
        manifest: ResolutionManifest {
            resolutions,
            counts: PartialSwapResolutionCounts {
                chunks: plan.partial_swaps.len(),
                symbols: total_symbols,
                references_rewritten: outcome.total_references,
            },
        },
    })
}

/// Apply the plan's bundled partial swaps: write the planned bundle
/// copy + facades, rewrite consumer imports / references against the
/// facades, and seed the vendor chunk's self-rewrite. Validation,
/// facade planning, and resolution happened at plan time.
pub fn apply_bundled_partial_vendor_swaps(
    mut artifact: ChunkBundle,
    plan: &VendorResolutionPlan,
    references: &ArtifactIndexes,
    write: bool,
) -> Result<ApplyBundledPartialVendorSwapsResult> {
    let chunk_table = artifact.chunk_table.clone();
    let mut resolutions: BTreeMap<String, ChunkBundledPartialSwapResolution> = plan
        .bundled_partial_swaps
        .values()
        .map(|bundled| (bundled.chunk_path.clone(), bundled.resolution.clone()))
        .collect();

    if plan.bundled_partial_swaps.is_empty() {
        return Ok(ApplyBundledPartialVendorSwapsResult {
            artifact,
            manifest: ResolutionManifest {
                resolutions,
                counts: PartialSwapResolutionCounts {
                    chunks: 0,
                    symbols: 0,
                    references_rewritten: 0,
                },
            },
            self_rewrite_import_locals_by_chunk_path: BTreeMap::new(),
        });
    }

    if write {
        for bundled in plan.bundled_partial_swaps.values() {
            write_planned_bundled_assets(&bundled.assets)?;
        }
    }

    let outcome = dispatch_partial_swap_jobs(
        &mut artifact,
        &chunk_table,
        &plan.bundled_partial_swaps,
        references,
        &mut resolutions,
        rewrite_bundled_partial_swap_in_file,
    )?;

    let mut self_rewrite_import_locals_by_chunk_path: BTreeMap<String, BTreeSet<Id>> =
        BTreeMap::new();
    for (caller_chunk_id, locals) in &outcome.self_rewrite_locals_by_caller {
        if let Some(bundled) = plan.bundled_partial_swaps.get(caller_chunk_id) {
            self_rewrite_import_locals_by_chunk_path
                .entry(bundled.chunk_path.clone())
                .or_default()
                .extend(locals.clone());
        }
    }

    let total_symbols: usize = resolutions
        .values()
        .map(|resolution| resolution.symbols.len())
        .sum();
    Ok(ApplyBundledPartialVendorSwapsResult {
        artifact,
        manifest: ResolutionManifest {
            resolutions,
            counts: PartialSwapResolutionCounts {
                chunks: plan.bundled_partial_swaps.len(),
                symbols: total_symbols,
                references_rewritten: outcome.total_references,
            },
        },
        self_rewrite_import_locals_by_chunk_path,
    })
}

struct PartialSwapFileJob {
    caller_chunk_index: usize,
    caller_chunk_id: ChunkId,
    file_path: String,
    parts: JsFileAstParts,
    ast: ParsedJsModule,
}

struct PartialSwapFileResult {
    caller_chunk_index: usize,
    caller_chunk_id: ChunkId,
    parts: JsFileAstParts,
    ast: ParsedJsModule,
    /// (chunk, chunk_export) -> count of rewritten references in
    /// this file.
    references_by_symbol: BTreeMap<(ChunkId, String), usize>,
    self_rewrite_import_locals: BTreeSet<Id>,
}

fn rewrite_partial_swap_in_file(
    mut job: PartialSwapFileJob,
    references: &ArtifactIndexes,
    mappings: &BTreeMap<ChunkId, ChunkPartialSwapPlan>,
    chunk_table: &ChunkTable,
    materialized_index: &MaterializedOutputChunkIndex,
) -> PartialSwapFileResult {
    let module = &mut job.ast.module;

    // Pass A: scan ImportDecls. For each ImportSpecifier::Named on a
    // partial-swap chunk import:
    //   - kind=member: record the local-binding sym for Pass B's
    //     member-access rewrite; queue one shared
    //     `import * as <pkg.namespace> from "<pkg>"` for the file.
    //   - kind=namespace: queue a per-binding
    //     `import * as <local> from "<pkg>"`; no identifier rewrite.
    //   - kind=default: queue a per-binding
    //     `import <local> from "<pkg>"`; no identifier rewrite.
    let mut bindings: BTreeMap<Id, IdentRewriteTarget> = BTreeMap::new();
    let mut emitted_member_namespace_for: BTreeSet<String> = BTreeSet::new();
    let mut references_by_symbol: BTreeMap<(ChunkId, String), usize> = BTreeMap::new();

    module.body = rewrite_swap_import_decls(
        std::mem::take(&mut module.body),
        job.caller_chunk_id,
        &job.file_path,
        references,
        chunk_table,
        materialized_index,
        |target_chunk_id, imported_name_lookup, local_sym| {
            let chunk_mapping = mappings.get(&target_chunk_id)?;
            let target = chunk_mapping.symbols.get(imported_name_lookup)?;
            let package_coords = chunk_mapping.packages.get(&target.package)?;
            let mut imports = Vec::new();
            match target.kind {
                PartialSwapKind::Member => {
                    let upstream_export = target.upstream_export.as_deref()?;
                    let namespace = package_coords.namespace.as_deref()?;
                    bindings.insert(
                        local_sym.to_id(),
                        IdentRewriteTarget::Member {
                            namespace: namespace.to_string(),
                            upstream_export: upstream_export.to_string(),
                            chunk_id: target_chunk_id,
                            chunk_export: imported_name_lookup.to_string(),
                        },
                    );
                    if emitted_member_namespace_for.insert(target.package.clone()) {
                        imports.push(DeferredImport::Namespace {
                            source: target.package.clone(),
                            local: namespace.to_string(),
                        });
                    }
                }
                PartialSwapKind::Namespace => {
                    imports.push(DeferredImport::Namespace {
                        source: target.package.clone(),
                        local: local_sym.sym.to_string(),
                    });
                    *references_by_symbol
                        .entry((target_chunk_id, imported_name_lookup.to_string()))
                        .or_insert(0) += 1;
                }
                PartialSwapKind::Default => {
                    imports.push(DeferredImport::Default {
                        source: target.package.clone(),
                        local: local_sym.sym.to_string(),
                    });
                    *references_by_symbol
                        .entry((target_chunk_id, imported_name_lookup.to_string()))
                        .or_insert(0) += 1;
                }
                PartialSwapKind::Named => {
                    let upstream_export = target.upstream_export.as_deref()?;
                    imports.push(DeferredImport::Named {
                        source: target.package.clone(),
                        local: upstream_export.to_string(),
                        upstream_export: upstream_export.to_string(),
                    });
                    if local_sym.sym.as_ref() != upstream_export {
                        bindings.insert(
                            local_sym.to_id(),
                            IdentRewriteTarget::Rename {
                                upstream_export: upstream_export.to_string(),
                                chunk_id: target_chunk_id,
                                chunk_export: imported_name_lookup.to_string(),
                            },
                        );
                    } else {
                        *references_by_symbol
                            .entry((target_chunk_id, imported_name_lookup.to_string()))
                            .or_insert(0) += 1;
                    }
                }
            }
            Some(imports)
        },
    );

    // Pass A2: rewrite `export { x } from "<vendor-chunk>"` re-exports of
    // swapped names. The strip pass removes those names from the chunk's
    // export surface, so an unrewritten re-export would link-fail.
    module.body = rewrite_partial_swap_export_from_decls(
        std::mem::take(&mut module.body),
        job.caller_chunk_id,
        &job.file_path,
        references,
        chunk_table,
        materialized_index,
        mappings,
        &mut references_by_symbol,
    );

    // Pass B: rewrite every Expr::Ident reference to a tracked local
    // binding into `<namespace>.<upstream_export>`. Only kind=member
    // populates `bindings`; kind=namespace and kind=default leave the
    // local-binding references intact.
    if !bindings.is_empty() {
        let mut rewriter = PartialSwapIdentRewriter {
            bindings: &bindings,
            references_by_symbol: &mut references_by_symbol,
        };
        module.visit_mut_with(&mut rewriter);
    }

    PartialSwapFileResult {
        caller_chunk_index: job.caller_chunk_index,
        caller_chunk_id: job.caller_chunk_id,
        parts: job.parts,
        ast: job.ast,
        references_by_symbol,
        self_rewrite_import_locals: BTreeSet::new(),
    }
}

/// Rewrite `export { <chunk_export> as <name> } from "<vendor-chunk>"`
/// re-exports of swapped names against the upstream package:
///
/// * `kind: named`     → `export { <upstream_export> as <name> } from "<pkg>"`
/// * `kind: default`   → `export { default as <name> } from "<pkg>"`
/// * `kind: namespace` → `export * as <name> from "<pkg>"`
/// * `kind: member`    → retained — a member access off a namespace import
///   has no live re-export equivalent; the post-strip consumer gate bails
///   with a precise diagnostic instead.
///
/// Bundled partial swaps are not rewritten here (their facade default is
/// only member-addressable); the consumer gate covers them.
#[allow(clippy::too_many_arguments)] // mirrors rewrite_swap_import_decls' resolution context
fn rewrite_partial_swap_export_from_decls(
    original_body: Vec<ModuleItem>,
    caller_chunk_id: ChunkId,
    caller_file_path: &str,
    references: &ArtifactIndexes,
    chunk_table: &ChunkTable,
    materialized_index: &MaterializedOutputChunkIndex,
    mappings: &BTreeMap<ChunkId, ChunkPartialSwapPlan>,
    references_by_symbol: &mut BTreeMap<(ChunkId, String), usize>,
) -> Vec<ModuleItem> {
    let mut new_body: Vec<ModuleItem> = Vec::with_capacity(original_body.len());
    for item in original_body {
        let ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(mut named)) = item else {
            new_body.push(item);
            continue;
        };
        let Some(src) = named.src.as_deref() else {
            new_body.push(ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)));
            continue;
        };
        let source = str_value(src);
        let Some(target_chunk_id) = resolve_partial_swap_import_target(
            &source,
            caller_chunk_id,
            caller_file_path,
            references,
            chunk_table,
            materialized_index,
        ) else {
            new_body.push(ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)));
            continue;
        };
        let chunk_mapping = if target_chunk_id == caller_chunk_id {
            None
        } else {
            mappings.get(&target_chunk_id)
        };
        let Some(chunk_mapping) = chunk_mapping else {
            new_body.push(ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)));
            continue;
        };
        let mut retained: Vec<ExportSpecifier> = Vec::new();
        let mut replacements: Vec<ModuleItem> = Vec::new();
        for specifier in std::mem::take(&mut named.specifiers) {
            let ExportSpecifier::Named(named_spec) = &specifier else {
                retained.push(specifier);
                continue;
            };
            let orig_name = module_export_name(&named_spec.orig);
            let exported_name = named_spec
                .exported
                .as_ref()
                .map(module_export_name)
                .unwrap_or_else(|| orig_name.clone());
            let Some(target) = chunk_mapping.symbols.get(&orig_name) else {
                retained.push(specifier);
                continue;
            };
            if !is_valid_identifier(&exported_name) {
                retained.push(specifier);
                continue;
            }
            let replacement = match target.kind {
                PartialSwapKind::Named => target
                    .upstream_export
                    .as_deref()
                    .map(|upstream| make_named_reexport(&target.package, upstream, &exported_name)),
                PartialSwapKind::Default => Some(make_named_reexport(
                    &target.package,
                    "default",
                    &exported_name,
                )),
                PartialSwapKind::Namespace => {
                    Some(make_namespace_reexport(&target.package, &exported_name))
                }
                PartialSwapKind::Member => None,
            };
            let Some(replacement) = replacement else {
                retained.push(specifier);
                continue;
            };
            replacements.push(replacement);
            *references_by_symbol
                .entry((target_chunk_id, orig_name))
                .or_insert(0) += 1;
        }
        new_body.extend(replacements);
        if !retained.is_empty() {
            named.specifiers = retained;
            new_body.push(ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)));
        }
    }
    new_body
}

fn rewrite_bundled_partial_swap_in_file(
    mut job: PartialSwapFileJob,
    references: &ArtifactIndexes,
    mappings: &BTreeMap<ChunkId, ChunkBundledPartialSwapPlan>,
    chunk_table: &ChunkTable,
    materialized_index: &MaterializedOutputChunkIndex,
) -> PartialSwapFileResult {
    let module = &mut job.ast.module;

    let mut bindings: BTreeMap<Id, IdentRewriteTarget> = BTreeMap::new();
    let mut emitted_default_namespace_for: BTreeSet<String> = BTreeSet::new();
    let mut references_by_symbol: BTreeMap<(ChunkId, String), usize> = BTreeMap::new();
    let mut prelude_imports: Vec<DeferredImport> = Vec::new();
    let mut self_rewrite_import_locals: BTreeSet<Id> = BTreeSet::new();
    seed_bundled_partial_swap_self_rewrites(
        module,
        mappings.get(&job.caller_chunk_id),
        chunk_table,
        job.caller_chunk_id,
        &job.file_path,
        SelfRewriteOutputs {
            bindings: &mut bindings,
            prelude_imports: &mut prelude_imports,
            references_by_symbol: &mut references_by_symbol,
            self_rewrite_import_locals: &mut self_rewrite_import_locals,
        },
    );

    let new_body = rewrite_swap_import_decls(
        std::mem::take(&mut module.body),
        job.caller_chunk_id,
        &job.file_path,
        references,
        chunk_table,
        materialized_index,
        |target_chunk_id, imported_name_lookup, local_sym| {
            let chunk_mapping = mappings.get(&target_chunk_id)?;
            let target = chunk_mapping.symbols.get(imported_name_lookup)?;
            let package_coords = chunk_mapping.packages.get(&target.package)?;
            let import_source = bundled_facade_import_source(
                chunk_table,
                job.caller_chunk_id,
                &job.file_path,
                &package_coords.facade_app_path,
            );
            let mut imports = Vec::new();
            match target.kind {
                PartialSwapKind::Member | PartialSwapKind::Named => {
                    let upstream_export = target.upstream_export.as_deref()?;
                    let namespace = package_coords.namespace.as_deref()?;
                    bindings.insert(
                        local_sym.to_id(),
                        IdentRewriteTarget::Member {
                            namespace: namespace.to_string(),
                            upstream_export: upstream_export.to_string(),
                            chunk_id: target_chunk_id,
                            chunk_export: imported_name_lookup.to_string(),
                        },
                    );
                    if emitted_default_namespace_for.insert(target.package.clone()) {
                        imports.push(DeferredImport::Default {
                            source: import_source,
                            local: namespace.to_string(),
                        });
                    }
                }
                PartialSwapKind::Namespace | PartialSwapKind::Default => {
                    imports.push(DeferredImport::Default {
                        source: import_source,
                        local: local_sym.sym.to_string(),
                    });
                    *references_by_symbol
                        .entry((target_chunk_id, imported_name_lookup.to_string()))
                        .or_insert(0) += 1;
                }
            }
            Some(imports)
        },
    );

    if !prelude_imports.is_empty() {
        let mut prefixed = prelude_imports
            .drain(..)
            .map(DeferredImport::into_module_item)
            .collect::<Vec<_>>();
        prefixed.extend(new_body);
        module.body = prefixed;
    } else {
        module.body = new_body;
    }

    if !bindings.is_empty() {
        let mut rewriter = PartialSwapIdentRewriter {
            bindings: &bindings,
            references_by_symbol: &mut references_by_symbol,
        };
        module.visit_mut_with(&mut rewriter);
    }

    PartialSwapFileResult {
        caller_chunk_index: job.caller_chunk_index,
        caller_chunk_id: job.caller_chunk_id,
        parts: job.parts,
        ast: job.ast,
        references_by_symbol,
        self_rewrite_import_locals,
    }
}

fn rewrite_swap_import_decls<F>(
    original_body: Vec<ModuleItem>,
    caller_chunk_id: ChunkId,
    caller_file_path: &str,
    references: &ArtifactIndexes,
    chunk_table: &ChunkTable,
    materialized_index: &MaterializedOutputChunkIndex,
    mut rewrite_one: F,
) -> Vec<ModuleItem>
where
    F: FnMut(ChunkId, &str, &Ident) -> Option<Vec<DeferredImport>>,
{
    let mut new_body: Vec<ModuleItem> = Vec::with_capacity(original_body.len() + 4);
    for item in original_body {
        let ModuleItem::ModuleDecl(ModuleDecl::Import(mut import_decl)) = item else {
            new_body.push(item);
            continue;
        };
        let source = str_value(&import_decl.src);
        let Some(target_chunk_id) = resolve_partial_swap_import_target(
            &source,
            caller_chunk_id,
            caller_file_path,
            references,
            chunk_table,
            materialized_index,
        ) else {
            new_body.push(ModuleItem::ModuleDecl(ModuleDecl::Import(import_decl)));
            continue;
        };
        if target_chunk_id == caller_chunk_id {
            new_body.push(ModuleItem::ModuleDecl(ModuleDecl::Import(import_decl)));
            continue;
        }
        let mut retained_specifiers: Vec<ImportSpecifier> = Vec::new();
        let mut decl_local_imports: Vec<DeferredImport> = Vec::new();
        for specifier in std::mem::take(&mut import_decl.specifiers) {
            let imported_name_lookup = match &specifier {
                ImportSpecifier::Named(named) => named
                    .imported
                    .as_ref()
                    .map(module_export_name)
                    .unwrap_or_else(|| named.local.sym.to_string()),
                _ => {
                    retained_specifiers.push(specifier);
                    continue;
                }
            };
            let ImportSpecifier::Named(named) = specifier else {
                unreachable!("classified named above");
            };
            if let Some(imports) = rewrite_one(target_chunk_id, &imported_name_lookup, &named.local)
            {
                decl_local_imports.extend(imports);
            } else {
                retained_specifiers.push(ImportSpecifier::Named(named));
            }
        }
        for deferred in decl_local_imports.drain(..) {
            new_body.push(deferred.into_module_item());
        }
        if !retained_specifiers.is_empty() {
            import_decl.specifiers = retained_specifiers;
            new_body.push(ModuleItem::ModuleDecl(ModuleDecl::Import(import_decl)));
        }
    }
    new_body
}

/// Map each chunk-local named export to the hygiene-preserving `Id`
/// (`(atom, SyntaxContext)`) of the binding it re-exports. The `orig`
/// identifier carries the resolver-assigned `SyntaxContext`, so the
/// returned `Id` is the canonical binding identity used to key the
/// self-rewrite `bindings` map.
fn collect_local_idents_by_export_name(module: &Module) -> BTreeMap<String, Id> {
    let mut out = BTreeMap::new();
    for item in &module.body {
        let ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) = item else {
            continue;
        };
        if named.src.is_some() {
            continue;
        }
        for spec in &named.specifiers {
            let ExportSpecifier::Named(named_spec) = spec else {
                continue;
            };
            let ModuleExportName::Ident(orig) = &named_spec.orig else {
                continue;
            };
            let export_name = named_spec
                .exported
                .as_ref()
                .map(module_export_name)
                .unwrap_or_else(|| orig.sym.to_string());
            out.insert(export_name, orig.to_id());
        }
    }
    out
}

/// Map each top-level binding name to its hygiene-preserving `Id`
/// (`(atom, SyntaxContext)`). Used to resolve a manifest-recorded
/// `local` name (a bare string) to the actual binding cell, so the
/// self-rewrite map keys on binding identity rather than bare text.
/// If two top-level declarations share a name (illegal in module
/// scope after resolver, but defensive), the last one wins.
fn collect_top_level_binding_ids(module: &Module) -> BTreeMap<String, Id> {
    let mut out = BTreeMap::new();
    for item in &module.body {
        let decl = match item {
            ModuleItem::Stmt(Stmt::Decl(decl)) => decl,
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => &export_decl.decl,
            _ => continue,
        };
        for id in declaration_ids(decl) {
            out.insert(id.0.to_string(), id);
        }
    }
    out
}

struct SelfRewriteOutputs<'a> {
    bindings: &'a mut BTreeMap<Id, IdentRewriteTarget>,
    prelude_imports: &'a mut Vec<DeferredImport>,
    references_by_symbol: &'a mut BTreeMap<(ChunkId, String), usize>,
    self_rewrite_import_locals: &'a mut BTreeSet<Id>,
}

fn seed_bundled_partial_swap_self_rewrites(
    module: &Module,
    chunk_mapping: Option<&ChunkBundledPartialSwapPlan>,
    chunk_table: &ChunkTable,
    caller_chunk_id: ChunkId,
    caller_file_path: &str,
    outputs: SelfRewriteOutputs<'_>,
) {
    let Some(chunk_mapping) = chunk_mapping else {
        return;
    };
    let exported_locals = collect_local_idents_by_export_name(module);
    // The manifest records `local` only as a bare name string. Resolve
    // it to the hygiene-preserving binding `Id` of the matching
    // top-level declaration so the self-rewrite map is keyed on binding
    // identity (matching `ident.to_id()` in the rewriter), not bare
    // text — otherwise a same-named binding in a nested scope would be
    // miscompiled.
    let top_level_bindings = collect_top_level_binding_ids(module);
    let mut used_idents = module_used_idents(module);
    for (chunk_export, target) in &chunk_mapping.symbols {
        let local_id = match &target.local {
            Some(local_name) => top_level_bindings.get(local_name),
            None => exported_locals.get(chunk_export),
        };
        let Some(local_id) = local_id else {
            continue;
        };
        let Some(package_coords) = chunk_mapping.packages.get(&target.package) else {
            continue;
        };
        let import_source = bundled_facade_import_source(
            chunk_table,
            caller_chunk_id,
            caller_file_path,
            &package_coords.facade_app_path,
        );
        let local =
            unique_synthetic_ident(&format!("__debundle_bps_{chunk_export}"), &mut used_idents);
        match target.kind {
            PartialSwapKind::Member | PartialSwapKind::Named => {
                let upstream_export = target
                    .upstream_export
                    .as_deref()
                    .expect("kind=member/named validated to carry upstream_export");
                outputs.bindings.insert(
                    local_id.clone(),
                    IdentRewriteTarget::Member {
                        namespace: local.clone(),
                        upstream_export: upstream_export.to_string(),
                        chunk_id: caller_chunk_id,
                        chunk_export: chunk_export.clone(),
                    },
                );
            }
            PartialSwapKind::Namespace | PartialSwapKind::Default => {
                outputs.bindings.insert(
                    local_id.clone(),
                    IdentRewriteTarget::Rename {
                        upstream_export: local.clone(),
                        chunk_id: caller_chunk_id,
                        chunk_export: chunk_export.clone(),
                    },
                );
                *outputs
                    .references_by_symbol
                    .entry((caller_chunk_id, chunk_export.clone()))
                    .or_insert(0) += 1;
            }
        }
        outputs
            .self_rewrite_import_locals
            .insert(Ident::new_no_ctxt(local.clone().into(), DUMMY_SP).to_id());
        outputs.prelude_imports.push(DeferredImport::Default {
            source: import_source,
            local,
        });
    }
}

fn unique_synthetic_ident(base: &str, used: &mut BTreeSet<String>) -> String {
    if used.insert(base.to_string()) {
        return base.to_string();
    }
    let mut i = 2usize;
    loop {
        let candidate = format!("{base}_{i}");
        if used.insert(candidate.clone()) {
            return candidate;
        }
        i += 1;
    }
}

fn module_used_idents(module: &Module) -> BTreeSet<String> {
    struct UsedIdentCollector(BTreeSet<String>);
    impl Visit for UsedIdentCollector {
        fn visit_ident(&mut self, ident: &Ident) {
            self.0.insert(ident.sym.to_string());
        }
    }
    let mut collector = UsedIdentCollector(BTreeSet::new());
    module.visit_with(&mut collector);
    collector.0
}

fn bundled_facade_import_source(
    chunk_table: &ChunkTable,
    caller_chunk_id: ChunkId,
    caller_file_path: &str,
    facade_app_path: &str,
) -> String {
    let caller_output_file = Path::new(chunk_table.name(caller_chunk_id)).join(caller_file_path);
    let caller_output_dir = caller_output_file.parent().unwrap_or_else(|| Path::new(""));
    relative_module_specifier(caller_output_dir, Path::new(facade_app_path))
}

fn resolve_partial_swap_import_target(
    source: &str,
    caller_chunk_id: ChunkId,
    caller_file_path: &str,
    references: &ArtifactIndexes,
    chunk_table: &ChunkTable,
    materialized_index: &MaterializedOutputChunkIndex,
) -> Option<ChunkId> {
    references
        .resolve_runtime_import_reference(source, caller_chunk_id, caller_file_path, chunk_table)
        .map(|resolved| resolved.target_chunk_id)
        .or_else(|| {
            resolve_materialized_output_import_target(
                source,
                caller_chunk_id,
                caller_file_path,
                chunk_table,
                materialized_index,
            )
        })
}

fn resolve_materialized_output_import_target(
    source: &str,
    caller_chunk_id: ChunkId,
    caller_file_path: &str,
    chunk_table: &ChunkTable,
    materialized_index: &MaterializedOutputChunkIndex,
) -> Option<ChunkId> {
    if source.is_empty() || !source.starts_with('.') {
        return None;
    }
    let caller_output_dir = join_module_path(&[
        chunk_table.name(caller_chunk_id),
        module_path_dirname(caller_file_path).as_str(),
    ]);
    let resolved_path =
        normalize_module_path(&join_module_path(&[caller_output_dir.as_str(), source])).ok()?;
    materialized_index.lookup(&resolved_path)
}

/// Precomputed longest-prefix-match index for resolving partial-swap
/// relative imports to their target chunk. Built once per
/// `apply_*partial_vendor_swaps` invocation; replaces the per-import-decl
/// O(N_chunks) scan over `ChunkTable`.
///
/// Per chunk we register candidate keys derived from the chunk name plus,
/// for slash-bearing names, the post-first-slash stripped form (mirroring
/// the original `materialized_output_chunk_match_len` two-shape match):
///   * `by_exact["<name>.js"]`         — exact match against
///     `resolved_path` (match_len = name.len()).
///   * `by_dir_prefix["<name>"]`       — `resolved_path` is
///     `"<name>/<rest>"` (match_len = name.len()).
///
/// Lookup checks the exact map plus walks `resolved_path`'s `/`-bounded
/// ancestors longest-to-shortest against `by_dir_prefix`, then combines
/// the two candidates with longest-match-wins / tie-breaks-as-`None`
/// (same semantics as the prior linear scan, including `ambiguous`).
struct MaterializedOutputChunkIndex {
    by_exact: HashMap<String, ChunkEntry>,
    by_dir_prefix: HashMap<String, ChunkEntry>,
}

#[derive(Clone, Copy)]
enum ChunkEntry {
    Unique(ChunkId, usize),
    Ambiguous(usize),
}

impl ChunkEntry {
    fn match_len(&self) -> usize {
        match self {
            ChunkEntry::Unique(_, len) | ChunkEntry::Ambiguous(len) => *len,
        }
    }

    fn merge(&mut self, chunk_id: ChunkId, match_len: usize) {
        match *self {
            ChunkEntry::Unique(existing, existing_len) => {
                debug_assert_eq!(existing_len, match_len);
                if existing != chunk_id {
                    *self = ChunkEntry::Ambiguous(match_len);
                }
            }
            ChunkEntry::Ambiguous(existing_len) => {
                debug_assert_eq!(existing_len, match_len);
            }
        }
    }
}

impl MaterializedOutputChunkIndex {
    fn build(chunk_table: &ChunkTable) -> Self {
        let len = chunk_table.len();
        let mut by_exact: HashMap<String, ChunkEntry> = HashMap::with_capacity(len * 2);
        let mut by_dir_prefix: HashMap<String, ChunkEntry> = HashMap::with_capacity(len * 2);
        for index in 0..len {
            let chunk_id = ChunkId(index);
            let chunk_name = chunk_table.name(chunk_id);
            insert_candidate(&mut by_exact, &mut by_dir_prefix, chunk_id, chunk_name);
            if let Some((_, stripped)) = chunk_name.split_once('/') {
                insert_candidate(&mut by_exact, &mut by_dir_prefix, chunk_id, stripped);
            }
        }
        Self {
            by_exact,
            by_dir_prefix,
        }
    }

    fn lookup(&self, resolved_path: &str) -> Option<ChunkId> {
        let exact = self.by_exact.get(resolved_path).copied();
        let prefix = self.longest_prefix_match(resolved_path);
        let candidate = match (exact, prefix) {
            (None, None) => return None,
            (Some(c), None) | (None, Some(c)) => c,
            (Some(a), Some(b)) => {
                if a.match_len() > b.match_len() {
                    a
                } else if b.match_len() > a.match_len() {
                    b
                } else {
                    // Equal lengths: ambiguous unless both resolve to the
                    // same Unique chunk (only possible when an exact
                    // `"<name>.js"` key happens to also be a registered
                    // dir-prefix for the same chunk, which the original
                    // semantics never produced — but we keep the check
                    // explicit).
                    match (a, b) {
                        (ChunkEntry::Unique(ax, _), ChunkEntry::Unique(bx, _)) if ax == bx => a,
                        _ => return None,
                    }
                }
            }
        };
        match candidate {
            ChunkEntry::Unique(chunk_id, _) => Some(chunk_id),
            ChunkEntry::Ambiguous(_) => None,
        }
    }

    fn longest_prefix_match(&self, resolved_path: &str) -> Option<ChunkEntry> {
        // Walk `/`-bounded ancestors of `resolved_path` longest-first.
        // A by_dir_prefix entry `K` matches iff `resolved_path == "<K>/<rest>"`.
        let mut end = resolved_path.rfind('/')?;
        loop {
            if let Some(entry) = self.by_dir_prefix.get(&resolved_path[..end]) {
                return Some(*entry);
            }
            match resolved_path[..end].rfind('/') {
                Some(next) => end = next,
                None => return None,
            }
        }
    }
}

fn insert_candidate(
    by_exact: &mut HashMap<String, ChunkEntry>,
    by_dir_prefix: &mut HashMap<String, ChunkEntry>,
    chunk_id: ChunkId,
    name: &str,
) {
    let match_len = name.len();
    let exact_key = format!("{name}.js");
    by_exact
        .entry(exact_key)
        .and_modify(|e| e.merge(chunk_id, match_len))
        .or_insert(ChunkEntry::Unique(chunk_id, match_len));
    by_dir_prefix
        .entry(name.to_string())
        .and_modify(|e| e.merge(chunk_id, match_len))
        .or_insert(ChunkEntry::Unique(chunk_id, match_len));
}

enum DeferredImport {
    /// `import * as <local> from "<source>"`
    Namespace { source: String, local: String },
    /// `import <local> from "<source>"`
    Default { source: String, local: String },
    /// `import { <upstream_export> as <local> } from "<source>"`,
    /// or `import { <name> } from "<source>"` when local == upstream.
    Named {
        source: String,
        local: String,
        upstream_export: String,
    },
}

impl DeferredImport {
    fn into_module_item(self) -> ModuleItem {
        match self {
            DeferredImport::Namespace { source, local } => make_namespace_import(&source, &local),
            DeferredImport::Default { source, local } => make_default_import(&source, &local),
            DeferredImport::Named {
                source,
                local,
                upstream_export,
            } => make_named_import(&source, &local, &upstream_export),
        }
    }
}

#[derive(Debug, Clone)]
enum IdentRewriteTarget {
    /// kind=member: rewrite `<local>` references to `<namespace>.<upstream_export>`.
    Member {
        namespace: String,
        upstream_export: String,
        chunk_id: ChunkId,
        chunk_export: String,
    },
    /// kind=named auto-rename: rewrite `<local>` references to a bare
    /// `<upstream_export>` identifier (matching the new no-alias import).
    Rename {
        upstream_export: String,
        chunk_id: ChunkId,
        chunk_export: String,
    },
}

/// Rewrites references to a partial-swap import local into the
/// replacement facade access. `bindings` is keyed by the import
/// local's hygiene-preserving `Id` (`(atom, SyntaxContext)`), captured
/// from the actual binding `Ident` at construction. The module here has
/// been through SWC's `resolver` pass (see
/// `js_ast::parse_and_resolve`), so `ident.to_id()` is the canonical
/// binding identity. Keying on `Id` (rather than the bare textual
/// symbol) ensures a same-named binding in a nested scope — a function
/// parameter, a shadowing `const`/`let`, a `catch` binding — is left
/// untouched, since it carries a different `SyntaxContext`.
struct PartialSwapIdentRewriter<'a> {
    bindings: &'a BTreeMap<Id, IdentRewriteTarget>,
    references_by_symbol: &'a mut BTreeMap<(ChunkId, String), usize>,
}

impl VisitMut for PartialSwapIdentRewriter<'_> {
    fn visit_mut_call_expr(&mut self, call: &mut CallExpr) {
        if local_namespace_iife_target(call)
            .is_some_and(|target| self.bindings.contains_key(&target))
        {
            // Preserve TS namespace/enum initializer arguments such as
            // `Sa || (Sa = {})`. The strip pass recognizes that shape as
            // a local mutation island and can then drop the old vendor
            // implementation. Rewriting the read side first would turn it
            // into `<facade>.Enum || (Sa = {})`, making it look like a hard
            // residual side effect and potentially mutating the replacement
            // facade.
            if let Callee::Expr(callee) = &mut call.callee {
                callee.visit_mut_with(self);
            }
            for arg in call.args.iter_mut().skip(1) {
                arg.visit_mut_with(self);
            }
            return;
        }

        call.visit_mut_children_with(self);
    }

    fn visit_mut_expr(&mut self, expr: &mut Expr) {
        // Recurse first so nested matches (e.g., the obj of a member
        // expression, the callee of a call) are rewritten before we
        // inspect this node.
        expr.visit_mut_children_with(self);
        let Expr::Ident(ident) = expr else {
            return;
        };
        let Some(target) = self.bindings.get(&ident.to_id()) else {
            return;
        };
        let (chunk_id, chunk_export) = match target {
            IdentRewriteTarget::Member {
                namespace,
                upstream_export,
                chunk_id,
                chunk_export,
            } => {
                *expr = Expr::Member(MemberExpr {
                    span: DUMMY_SP,
                    obj: Box::new(Expr::Ident(Ident::new_no_ctxt(
                        namespace.clone().into(),
                        DUMMY_SP,
                    ))),
                    prop: MemberProp::Ident(IdentName::new(
                        upstream_export.clone().into(),
                        DUMMY_SP,
                    )),
                });
                (*chunk_id, chunk_export)
            }
            IdentRewriteTarget::Rename {
                upstream_export,
                chunk_id,
                chunk_export,
            } => {
                *expr = Expr::Ident(Ident::new_no_ctxt(upstream_export.clone().into(), DUMMY_SP));
                (*chunk_id, chunk_export)
            }
        };
        *self
            .references_by_symbol
            .entry((chunk_id, chunk_export.clone()))
            .or_insert(0) += 1;
    }
}

fn make_namespace_import(package: &str, namespace: &str) -> ModuleItem {
    ModuleItem::ModuleDecl(ModuleDecl::Import(ImportDecl {
        span: DUMMY_SP,
        specifiers: vec![ImportSpecifier::Namespace(ImportStarAsSpecifier {
            span: DUMMY_SP,
            local: Ident::new_no_ctxt(namespace.into(), DUMMY_SP),
        })],
        src: Box::new(Str {
            span: DUMMY_SP,
            value: package.into(),
            raw: None,
        }),
        type_only: false,
        with: None,
        phase: ImportPhase::Evaluation,
    }))
}

fn make_default_import(package: &str, local: &str) -> ModuleItem {
    ModuleItem::ModuleDecl(ModuleDecl::Import(ImportDecl {
        span: DUMMY_SP,
        specifiers: vec![ImportSpecifier::Default(ImportDefaultSpecifier {
            span: DUMMY_SP,
            local: Ident::new_no_ctxt(local.into(), DUMMY_SP),
        })],
        src: Box::new(Str {
            span: DUMMY_SP,
            value: package.into(),
            raw: None,
        }),
        type_only: false,
        with: None,
        phase: ImportPhase::Evaluation,
    }))
}

fn make_named_import(package: &str, local: &str, upstream_export: &str) -> ModuleItem {
    let imported = if local == upstream_export {
        None
    } else {
        Some(ModuleExportName::Ident(Ident::new_no_ctxt(
            upstream_export.into(),
            DUMMY_SP,
        )))
    };
    ModuleItem::ModuleDecl(ModuleDecl::Import(ImportDecl {
        span: DUMMY_SP,
        specifiers: vec![ImportSpecifier::Named(ImportNamedSpecifier {
            span: DUMMY_SP,
            local: Ident::new_no_ctxt(local.into(), DUMMY_SP),
            imported,
            is_type_only: false,
        })],
        src: Box::new(Str {
            span: DUMMY_SP,
            value: package.into(),
            raw: None,
        }),
        type_only: false,
        with: None,
        phase: ImportPhase::Evaluation,
    }))
}

/// `export { <orig> as <exported> } from "<source>"` (alias omitted when
/// the names match).
fn make_named_reexport(source: &str, orig: &str, exported: &str) -> ModuleItem {
    let exported_name = (orig != exported)
        .then(|| ModuleExportName::Ident(Ident::new_no_ctxt(exported.into(), DUMMY_SP)));
    ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(NamedExport {
        span: DUMMY_SP,
        specifiers: vec![ExportSpecifier::Named(ExportNamedSpecifier {
            span: DUMMY_SP,
            orig: ModuleExportName::Ident(Ident::new_no_ctxt(orig.into(), DUMMY_SP)),
            exported: exported_name,
            is_type_only: false,
        })],
        src: Some(Box::new(Str {
            span: DUMMY_SP,
            value: source.into(),
            raw: None,
        })),
        type_only: false,
        with: None,
    }))
}

/// `export * as <exported> from "<source>"`.
fn make_namespace_reexport(source: &str, exported: &str) -> ModuleItem {
    ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(NamedExport {
        span: DUMMY_SP,
        specifiers: vec![ExportSpecifier::Namespace(ExportNamespaceSpecifier {
            span: DUMMY_SP,
            name: ModuleExportName::Ident(Ident::new_no_ctxt(exported.into(), DUMMY_SP)),
        })],
        src: Some(Box::new(Str {
            span: DUMMY_SP,
            value: source.into(),
            raw: None,
        })),
        type_only: false,
        with: None,
    }))
}

/// Post-strip cross-chunk soundness gate for partial vendor swaps.
///
/// `apply_partial_vendor_swaps` / `apply_bundled_partial_vendor_swaps`
/// rewrite `ImportDecl` named specifiers (and, for non-bundled swaps,
/// rewritable `export … from` re-exports); the strip pass then removes
/// every swapped name from the vendor chunk's export surface. Any
/// consumer that survived those rewrites while still referencing the
/// stripped surface yields a broken emitted tree:
///
/// * a named import / re-export of a swapped name link-fails;
/// * a namespace import (`import * as M`) silently reads `undefined`
///   for swapped members;
/// * `export *` silently drops the swapped names from the re-exporter.
///
/// Scan every retained file of every chunk and bail with a precise
/// diagnostic on the first surviving consumer. Over-restriction is the
/// accepted failure mode: a namespace consumer that only reads
/// non-swapped members would work at runtime, but is rejected anyway
/// because per-member usage is not analyzed here.
pub fn validate_partial_swap_consumers(
    artifact: &ChunkBundle,
    plan: &VendorResolutionPlan,
    references: &ArtifactIndexes,
) -> Result<()> {
    let chunk_table = &artifact.chunk_table;
    let swapped_by_chunk: BTreeMap<ChunkId, BTreeSet<String>> = plan
        .partial_swaps
        .iter()
        .map(|(chunk_id, partial)| (*chunk_id, partial.symbols.keys().cloned().collect()))
        .chain(
            plan.bundled_partial_swaps
                .iter()
                .map(|(chunk_id, bundled)| (*chunk_id, bundled.symbols.keys().cloned().collect())),
        )
        .collect();
    if swapped_by_chunk.is_empty() {
        return Ok(());
    }
    let materialized_index = MaterializedOutputChunkIndex::build(chunk_table);
    for chunk_artifact in &artifact.chunks {
        let caller_chunk_id = chunk_artifact.chunk_id;
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
                let Some(swapped) = swapped_by_chunk.get(&target_chunk_id) else {
                    continue;
                };
                let consumer = format!("{caller_chunk_name}/{file_path}");
                check_partial_swap_consumer_decl(
                    decl,
                    swapped,
                    &consumer,
                    chunk_table.name(target_chunk_id),
                )?;
            }
        }
    }
    Ok(())
}

fn check_partial_swap_consumer_decl(
    decl: &ModuleDecl,
    swapped: &BTreeSet<String>,
    consumer: &str,
    target_chunk_name: &str,
) -> Result<()> {
    let swapped_list = || swapped.iter().cloned().collect::<Vec<_>>().join(",");
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
                        if swapped.contains(&imported) {
                            bail!(
                                "partial-swap consumer gate: {consumer} imports swapped name `{imported}` from partially-swapped vendor chunk {target_chunk_name}; the rewrite did not cover this consumer and the stripped chunk no longer exports it",
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
                        if swapped.contains("default") {
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
                        if swapped.contains(&orig) {
                            bail!(
                                "partial-swap consumer gate: {consumer} re-exports swapped name `{orig}` from partially-swapped vendor chunk {target_chunk_name}; this re-export shape has no live rewrite (kind=member symbols and bundled swaps cannot be expressed as re-exports) and the stripped chunk no longer exports it",
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
                        if swapped.contains("default") {
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

fn is_valid_identifier(name: &str) -> bool {
    let mut chars = name.chars();
    let Some(first) = chars.next() else {
        return false;
    };
    if !(first == '_' || first == '$' || first.is_ascii_alphabetic()) {
        return false;
    }
    chars.all(|ch| ch == '_' || ch == '$' || ch.is_ascii_alphanumeric())
}

#[cfg(test)]
mod tests {
    use super::*;
    use artifact::{
        ChunkAnalysisReport, ChunkArtifact, ChunkMetadata, FileMetadata, FileRole, JsChunk, JsFile,
    };
    use spec::{VendorLevel, VendorMark, VendorRole};

    #[test]
    fn rename_vendor_exports_rewrites_multiple_vendor_targets_in_one_call() {
        js_ast::with_swc_globals(|| {
            let mut artifact = ChunkBundle {
                chunks: Vec::new(),
                chunk_table: ChunkTable::default(),
            };
            insert_chunk(
                &mut artifact,
                "app",
                r#"import { a } from "../vendor-a/entry.js";
import { b as localB } from "../vendor-b/entry.js";
console.log(a, localB);
"#,
            );
            insert_chunk(
                &mut artifact,
                "vendor-a",
                r#"import { b } from "../vendor-b/entry.js";
const a = 1;
export { a as alpha };
console.log(b);
"#,
            );
            insert_chunk(
                &mut artifact,
                "vendor-b",
                r#"const b = 2;
export { b as beta };
"#,
            );

            let vendor = BTreeMap::from([
                (
                    "vendor-a.js".to_string(),
                    VendorMark {
                        identity: "a".to_string(),
                        role: VendorRole::Module,
                        level: VendorLevel::BoundaryRename,
                    },
                ),
                (
                    "vendor-b.js".to_string(),
                    VendorMark {
                        identity: "b".to_string(),
                        role: VendorRole::Module,
                        level: VendorLevel::BoundaryRename,
                    },
                ),
            ]);

            let references = ArtifactIndexes::build(&artifact).unwrap();
            let plan = build_vendor_resolution_plan(
                &artifact,
                &vendor,
                &VendorPlanOptions {
                    package_roots: &HashMap::new(),
                    packages_root: &None,
                    output_manifest_path: None,
                    output_wrapper_dir: None,
                },
            )
            .unwrap();
            let result = rename_vendor_exports(artifact, &plan, &references).unwrap();
            let artifact = result.artifact;
            let manifest = result.manifest;

            assert_eq!(manifest.counts.considered, 2);
            assert_eq!(manifest.counts.chunks_with_mapping, 2);
            assert_eq!(manifest.counts.rewrites, 3);
            assert_eq!(manifest.details[0].chunk_id, "vendor-a");
            assert_eq!(manifest.details[0].mapping_size, 1);
            assert_eq!(manifest.details[0].rewrites, 1);
            assert_eq!(manifest.details[0].callers[0].file, "app/entry.js");
            assert_eq!(manifest.details[1].chunk_id, "vendor-b");
            assert_eq!(manifest.details[1].mapping_size, 1);
            assert_eq!(manifest.details[1].rewrites, 2);
            assert_eq!(
                manifest.details[1]
                    .callers
                    .iter()
                    .map(|caller| (caller.file.as_str(), caller.rewrites))
                    .collect::<Vec<_>>(),
                vec![("app/entry.js", 1), ("vendor-a/entry.js", 1)]
            );

            assert_eq!(
                named_imports(&artifact, "app", "entry.js", "../vendor-a/entry.js"),
                vec![("alpha".to_string(), "a".to_string())]
            );
            assert_eq!(
                named_imports(&artifact, "app", "entry.js", "../vendor-b/entry.js"),
                vec![("beta".to_string(), "localB".to_string())]
            );
            assert_eq!(
                named_imports(&artifact, "vendor-a", "entry.js", "../vendor-b/entry.js"),
                vec![("beta".to_string(), "b".to_string())]
            );
        });
    }

    #[test]
    fn partial_swap_rewriter_preserves_namespace_iife_initializer_target() {
        js_ast::with_swc_globals(|| {
            let mut parsed = parse_js_module(
                "vendor.js",
                "var Sa;\n\
                 (function(t) { t[(t.NONE = 0)] = \"NONE\"; })(Sa || (Sa = {}));\n\
                 const direct = Sa.NONE;\n",
            )
            .unwrap();
            let sa_id = collect_top_level_binding_ids(&parsed.module)
                .remove("Sa")
                .expect("`Sa` is declared at top level");
            let bindings = BTreeMap::from([(
                sa_id,
                IdentRewriteTarget::Member {
                    namespace: "__debundle_bps_l3".to_string(),
                    upstream_export: "DiagLogLevel".to_string(),
                    chunk_id: ChunkId(0),
                    chunk_export: "l3".to_string(),
                },
            )]);
            let mut references_by_symbol = BTreeMap::new();
            parsed.module.visit_mut_with(&mut PartialSwapIdentRewriter {
                bindings: &bindings,
                references_by_symbol: &mut references_by_symbol,
            });

            let emitted = emit_js_module(&parsed, &[]).unwrap();
            assert!(
                emitted.contains("Sa || (Sa = {})"),
                "namespace IIFE target should stay recognizable for DCE:\n{emitted}",
            );
            assert!(
                emitted.contains("__debundle_bps_l3.DiagLogLevel.NONE"),
                "ordinary references should still be rewritten:\n{emitted}",
            );
        });
    }

    fn insert_chunk(artifact: &mut ChunkBundle, chunk_id: &str, source: &str) {
        let chunk_id_interned = artifact.chunk_table.intern(chunk_id.to_string());
        let entry_file = "entry.js".to_string();
        artifact.chunks.push(ChunkArtifact {
            chunk_id: chunk_id_interned,
            js: JsChunk {
                entry_file: entry_file.clone(),
                files: vec![JsFile {
                    path: entry_file.clone(),
                    body: artifact::JsFileBody::Ast(
                        parse_js_module(&format!("{chunk_id}/{entry_file}"), source).unwrap(),
                    ),
                    header_lines: Vec::new(),
                    binding_comments: std::collections::BTreeMap::new(),
                    leading_item_comments: std::collections::BTreeMap::new(),
                    metadata: FileMetadata {
                        chunk_id: chunk_id.to_string(),
                        chunk_file: entry_file.clone(),
                        role: FileRole::Entry,
                        source_path: format!("{chunk_id}.js"),
                    },
                }],
                metadata: ChunkMetadata {
                    source_path: format!("{chunk_id}.js"),
                },
            },
            analysis: ChunkAnalysisReport {
                chunk_id: chunk_id.to_string(),
                source_path: format!("{chunk_id}.js"),
                parser: Default::default(),
                entry_file,
                counts: Default::default(),
                files: Vec::new(),
                imports: Vec::new(),
                export_aliases: Vec::new(),
                unresolved_exports: Vec::new(),
                kept_top_level_declarations: Vec::new(),
            },
        });
    }

    fn named_imports(
        artifact: &ChunkBundle,
        chunk_id: &str,
        file: &str,
        source: &str,
    ) -> Vec<(String, String)> {
        let chunk_id_interned = artifact
            .chunk_table
            .get(chunk_id)
            .expect("chunk should exist");
        let module = &artifact
            .js_chunk(chunk_id_interned)
            .unwrap()
            .get_file(file)
            .unwrap()
            .ast()
            .unwrap()
            .module;
        module
            .body
            .iter()
            .find_map(|item| {
                let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item else {
                    return None;
                };
                (str_value(&import.src) == source).then(|| {
                    import
                        .specifiers
                        .iter()
                        .filter_map(|specifier| {
                            let ImportSpecifier::Named(named) = specifier else {
                                return None;
                            };
                            Some((
                                named
                                    .imported
                                    .as_ref()
                                    .map(module_export_name)
                                    .unwrap_or_else(|| named.local.sym.to_string()),
                                named.local.sym.to_string(),
                            ))
                        })
                        .collect()
                })
            })
            .unwrap_or_default()
    }

    fn chunk_table_with(names: &[&str]) -> ChunkTable {
        let mut t = ChunkTable::default();
        for n in names {
            t.intern((*n).to_string());
        }
        t
    }

    #[test]
    fn materialized_index_resolves_simple_name() {
        let table = chunk_table_with(&["app", "vendor"]);
        let index = MaterializedOutputChunkIndex::build(&table);
        assert_eq!(
            index.lookup("vendor.js"),
            Some(table.get("vendor").unwrap())
        );
        assert_eq!(
            index.lookup("vendor/entry.js"),
            Some(table.get("vendor").unwrap())
        );
        assert_eq!(
            index.lookup("app/entry.js"),
            Some(table.get("app").unwrap())
        );
        assert_eq!(index.lookup("missing.js"), None);
    }

    #[test]
    fn materialized_index_prefers_longer_prefix() {
        // Chunk "a/b" should win over "a" for path "a/b/x.js" because
        // match_len(3) > match_len(1).
        let table = chunk_table_with(&["a", "a/b"]);
        let index = MaterializedOutputChunkIndex::build(&table);
        assert_eq!(index.lookup("a/b/x.js"), Some(table.get("a/b").unwrap()));
        assert_eq!(index.lookup("a/x.js"), Some(table.get("a").unwrap()));
    }

    #[test]
    fn materialized_index_stripped_form_resolves() {
        // Chunk name "static/vendor" exposes stripped form "vendor"; a
        // path like "vendor.js" should resolve to that chunk.
        let table = chunk_table_with(&["static/vendor"]);
        let index = MaterializedOutputChunkIndex::build(&table);
        let target = table.get("static/vendor").unwrap();
        assert_eq!(index.lookup("static/vendor.js"), Some(target));
        assert_eq!(index.lookup("vendor.js"), Some(target));
        assert_eq!(index.lookup("vendor/foo.js"), Some(target));
    }

    #[test]
    fn materialized_index_ambiguous_returns_none() {
        // Both chunk "vendor" (exact name) and chunk "static/vendor"
        // (stripped form) match path "vendor.js" with match_len=6 →
        // ambiguous.
        let table = chunk_table_with(&["vendor", "static/vendor"]);
        let index = MaterializedOutputChunkIndex::build(&table);
        assert_eq!(index.lookup("vendor.js"), None);
        assert_eq!(index.lookup("vendor/foo.js"), None);
    }

    #[test]
    fn materialized_index_matches_legacy_linear_scan() {
        // Cross-check the index against the original O(N) scan logic for
        // a range of resolved paths, locking in the equivalence.
        fn legacy(resolved_path: &str, table: &ChunkTable) -> Option<ChunkId> {
            let mut best: Option<(usize, ChunkId)> = None;
            let mut ambiguous = false;
            for i in 0..table.len() {
                let cid = ChunkId(i);
                let name = table.name(cid);
                let mut len = None;
                if path_targets_legacy(resolved_path, name) {
                    len = Some(name.len());
                }
                if let Some((_, stripped)) = name.split_once('/')
                    && path_targets_legacy(resolved_path, stripped)
                    && len.is_none_or(|l| stripped.len() > l)
                {
                    len = Some(stripped.len());
                }
                let Some(ml) = len else {
                    continue;
                };
                match best {
                    None => {
                        best = Some((ml, cid));
                        ambiguous = false;
                    }
                    Some((bl, _)) if ml > bl => {
                        best = Some((ml, cid));
                        ambiguous = false;
                    }
                    Some((bl, bc)) if ml == bl && bc != cid => {
                        ambiguous = true;
                    }
                    _ => {}
                }
            }
            if ambiguous {
                None
            } else {
                best.map(|(_, c)| c)
            }
        }
        fn path_targets_legacy(resolved_path: &str, chunk_name: &str) -> bool {
            resolved_path == format!("{chunk_name}.js")
                || resolved_path
                    .strip_prefix(chunk_name)
                    .is_some_and(|rest| rest.starts_with('/'))
        }

        let table = chunk_table_with(&[
            "app",
            "vendor",
            "a/b",
            "a",
            "static/vendor",
            "deep/nested/chunk",
        ]);
        let index = MaterializedOutputChunkIndex::build(&table);
        for path in [
            "app.js",
            "app/main.js",
            "vendor.js",
            "vendor/x.js",
            "a/b/x.js",
            "a/x.js",
            "static/vendor.js",
            "static/vendor/foo.js",
            "deep/nested/chunk.js",
            "deep/nested/chunk/y.js",
            "unrelated.js",
            "deep/unrelated.js",
        ] {
            assert_eq!(
                index.lookup(path),
                legacy(path, &table),
                "mismatch on path={path:?}"
            );
        }
    }
}
