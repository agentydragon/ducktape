use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use rayon::prelude::*;
use serde::Serialize;
use serde_json::Value;
use swc_common::{DUMMY_SP, SyntaxContext};
use swc_ecma_ast::*;
use swc_ecma_visit::{VisitMut, VisitMutWith};

use artifact::{
    ArtifactIndexes, ChunkId, ChunkTable, JsFile, JsFileAstParts, JsPipelineArtifact,
    get_chunk_entry_path, list_chunk_file_paths, manifest_relative_path, path_from_module_path,
};
use js_ast::{ParsedJsModule, emit_js_module, parse_js_module, str_value};
use spec::{SwapMark, VendorLevel, VendorMark, VendorRole, WrapperShape};

// These manifests are returned by the vendor stages but the pipeline
// orchestrator only reads `kind` for stage logging. They are not
// serialized externally — drop `Serialize` to make that explicit.

#[derive(Debug, Clone)]
pub struct VendorAnnotationsManifest {
    pub counts: VendorAnnotationCounts,
    pub annotations: Vec<VendorAnnotationSummary>,
}

#[derive(Debug, Clone)]
pub struct VendorAnnotationCounts {
    pub annotations: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct VendorAnnotationSummary {
    pub chunk_path: String,
    pub chunk_id: String,
    pub identity: String,
    pub level: VendorAnnotationLevel,
    pub role: VendorRole,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub version: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub package: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub subpath: Option<String>,
}

#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum VendorAnnotationLevel {
    Suppress,
    BoundaryRename,
    Swap,
}

#[derive(Debug, Clone)]
pub struct RenameVendorExportsManifest {
    pub counts: RenameVendorExportsCounts,
    pub details: Vec<RenameVendorExportsDetail>,
}

pub struct RenameVendorExportsResult {
    pub artifact: JsPipelineArtifact,
    pub manifest: RenameVendorExportsManifest,
}

#[derive(Debug, Clone)]
pub struct RenameVendorExportsCounts {
    pub considered: usize,
    pub chunks_with_mapping: usize,
    pub rewrites: usize,
}

#[derive(Debug, Clone)]
pub struct RenameVendorExportsDetail {
    pub chunk_path: String,
    pub chunk_id: String,
    pub mapping_size: usize,
    pub rewrites: usize,
    pub callers: Vec<RenameVendorExportsCaller>,
}

#[derive(Debug, Clone)]
pub struct RenameVendorExportsCaller {
    pub file: String,
    pub rewrites: usize,
}

#[derive(Debug, Clone)]
pub struct VendorResolutionManifest {
    pub resolutions: BTreeMap<String, VendorResolution>,
    pub counts: VendorResolutionCounts,
}

pub struct SwapVendorChunksResult {
    pub artifact: JsPipelineArtifact,
    pub manifest: VendorResolutionManifest,
}

#[derive(Debug, Clone, Serialize)]
pub struct VendorResolution {
    pub chunk_id: String,
    pub chunk_path: String,
    pub entry_file: String,
    pub package: String,
    pub version: String,
    pub subpath: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub wrapper_shape: Option<WrapperShape>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub generated_wrapper_path: Option<String>,
}

#[derive(Debug, Clone)]
pub struct VendorResolutionCounts {
    pub swapped: usize,
}

#[derive(Debug, Clone)]
pub struct SwapVendorOptions<'a> {
    pub package_roots: &'a std::collections::HashMap<String, PathBuf>,
    pub packages_root: &'a Option<PathBuf>,
    pub output_manifest_path: Option<PathBuf>,
    pub output_wrapper_dir: Option<PathBuf>,
    pub write: bool,
}

pub fn apply_vendor_annotations(
    artifact: &JsPipelineArtifact,
    vendor: &BTreeMap<String, VendorMark>,
) -> Result<VendorAnnotationsManifest> {
    let mut summaries = Vec::with_capacity(vendor.len());
    for (chunk_path, mark) in vendor {
        let chunk_name = chunk_id_from_chunk_path(chunk_path, "mark_vendor")?;
        let chunk_id = artifact.chunk_table.get(&chunk_name).with_context(|| {
            format!("vendor entry {chunk_path} targets unknown chunk: {chunk_name}")
        })?;
        if get_chunk_entry_path(artifact, chunk_id).is_none() {
            bail!("vendor entry {chunk_path} targets missing chunk (chunk_id={chunk_name})");
        }
        let (level, package, version, subpath) = match &mark.level {
            VendorLevel::Swap(swap) => (
                VendorAnnotationLevel::Swap,
                Some(swap.package.clone()),
                Some(swap.version.clone()),
                Some(swap.subpath.clone()),
            ),
            VendorLevel::BoundaryRename => {
                (VendorAnnotationLevel::BoundaryRename, None, None, None)
            }
            VendorLevel::Suppress => (VendorAnnotationLevel::Suppress, None, None, None),
        };
        summaries.push(VendorAnnotationSummary {
            chunk_path: chunk_path.clone(),
            chunk_id: chunk_name,
            identity: mark.identity.clone(),
            level,
            role: mark.role,
            version: version.clone(),
            package,
            subpath,
        });
    }

    Ok(VendorAnnotationsManifest {
        counts: VendorAnnotationCounts {
            annotations: vendor.len(),
        },
        annotations: summaries,
    })
}

pub fn rename_vendor_exports(
    mut artifact: JsPipelineArtifact,
    vendor: &BTreeMap<String, VendorMark>,
    references: &ArtifactIndexes,
) -> Result<RenameVendorExportsResult> {
    let mut ops = Vec::<RenameVendorExportOp>::new();
    let mut mappings = VendorExportMappings::new();
    let mut total_rewrites = 0usize;
    let mut chunks_with_mapping = 0usize;
    let mut details = Vec::new();
    let chunk_table = artifact.chunk_table.clone();

    for (chunk_path, _mark) in vendor.iter().filter(|(_, mark)| {
        matches!(
            mark.level,
            VendorLevel::BoundaryRename | VendorLevel::Swap(_)
        )
    }) {
        let chunk_path_owned = chunk_path.clone();
        let chunk_name = chunk_id_from_chunk_path(chunk_path, "rename_vendor_exports")?;
        let chunk_id = chunk_table.get(&chunk_name)
            .with_context(|| format!("rename_vendor_exports vendor entry {chunk_path} targets unknown chunk: {chunk_name}"))?;
        let vendor_entry_relative_file =
            get_chunk_entry_path(&artifact, chunk_id).with_context(|| {
            format!(
                "rename_vendor_exports vendor entry {chunk_path_owned} targets missing chunk (chunk_id={chunk_name})"
            )
        })?;
        let mapping = {
            let vendor_ast = artifact
                .js_chunk(chunk_id)?
                .get_file(&vendor_entry_relative_file)
                .and_then(|file| file.ast())
                .with_context(|| {
                    format!("rename_vendor_exports vendor chunk {chunk_name} is missing entry AST")
                })?;
            collect_boundary_mapping(&vendor_ast.module)
        };
        if !mapping.is_empty() {
            chunks_with_mapping += 1;
            mappings
                .entry(chunk_name.clone())
                .or_default()
                .insert(vendor_entry_relative_file.clone(), mapping.clone());
        }
        ops.push(RenameVendorExportOp {
            chunk_path: chunk_path_owned,
            chunk_name,
            entry_file: vendor_entry_relative_file,
            mapping,
        });
    }

    let mut caller_counts_by_target = BTreeMap::<(String, String), BTreeMap<String, usize>>::new();
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
        let results = jobs
            .into_par_iter()
            .map(|job| rename_vendor_imports_in_file(job, references, &mappings, chunk_table_ref))
            .collect::<Vec<_>>();
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

    let considered = ops.len();
    for op in ops {
        let caller_counts = caller_counts_by_target
            .remove(&(op.chunk_name.clone(), op.entry_file.clone()))
            .unwrap_or_default();
        let chunk_rewrites = caller_counts.values().sum();
        details.push(RenameVendorExportsDetail {
            chunk_path: op.chunk_path,
            chunk_id: op.chunk_name,
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

type VendorExportMappings = BTreeMap<String, BTreeMap<String, BTreeMap<String, String>>>;

struct RenameVendorExportOp {
    chunk_path: String,
    chunk_name: String,
    entry_file: String,
    mapping: BTreeMap<String, String>,
}

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
    rewrites_by_target: BTreeMap<(String, String), usize>,
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

struct SwapVendorJob {
    chunk_path: String,
    chunk_name: String,
    entry_file: String,
    package: String,
    version: String,
    subpath: String,
    wrapper_shape: Option<WrapperShape>,
    vendor_exports: BTreeSet<String>,
}

fn resolve_vendor_swap(
    job: SwapVendorJob,
    import_alignment_index: &BTreeMap<String, Vec<ImportAlignmentRecord>>,
    options: &SwapVendorOptions<'_>,
) -> Result<VendorResolution> {
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
    let mut generated_wrapper_path = None::<PathBuf>;

    match job.wrapper_shape {
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
            let wrapper = generate_named_from_default_wrapper(&upstream_ast, &non_default_exports)?;
            generated_wrapper_path = write_wrapper_if_requested(
                options.write,
                options.output_wrapper_dir.as_deref(),
                &job.chunk_name,
                &job.entry_file,
                &wrapper,
            )?;
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
            let wrapper =
                generate_named_from_json_default_wrapper(&upstream_json, &non_default_exports);
            generated_wrapper_path = write_wrapper_if_requested(
                options.write,
                options.output_wrapper_dir.as_deref(),
                &job.chunk_name,
                &job.entry_file,
                &wrapper,
            )?;
        }
        Some(WrapperShape::NamedFromModuleDefault) => {
            let upstream_ast =
                parse_js_module(&upstream_path.display().to_string(), &upstream_code)?;
            let wrapper = generate_named_from_module_default_wrapper(
                &upstream_ast,
                &job.vendor_exports,
                &job.chunk_path,
            )?;
            generated_wrapper_path = write_wrapper_if_requested(
                options.write,
                options.output_wrapper_dir.as_deref(),
                &job.chunk_name,
                &job.entry_file,
                &wrapper,
            )?;
        }
        None => {
            let upstream_ast =
                parse_js_module(&upstream_path.display().to_string(), &upstream_code)?;
            let upstream_exports = collect_exported_names(&upstream_ast.module, true);
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
        }
    }

    for record in import_alignment_index
        .get(&job.chunk_name)
        .into_iter()
        .flatten()
    {
        for imported_name in &record.named_imports {
            if job.vendor_exports.contains(imported_name) {
                continue;
            }
            bail!(
                "swap_vendor_chunks vendor entry {} import alignment failed: caller={}/{} imports unknown specifier \"{}\" from vendor {} (known: [{}])",
                job.chunk_path,
                record.caller_chunk_id,
                record.caller_file,
                imported_name,
                job.chunk_name,
                job.vendor_exports
                    .iter()
                    .cloned()
                    .collect::<Vec<_>>()
                    .join(",")
            );
        }
    }

    let generated_wrapper_path = generated_wrapper_path.as_ref().and_then(|path| {
        options
            .output_manifest_path
            .as_deref()
            .map(|manifest_path| manifest_relative_path(manifest_path, path))
    });
    Ok(VendorResolution {
        chunk_id: job.chunk_name,
        chunk_path: job.chunk_path,
        entry_file: job.entry_file,
        package: job.package,
        version: job.version,
        subpath: job.subpath,
        wrapper_shape: job.wrapper_shape,
        generated_wrapper_path,
    })
}

pub fn swap_vendor_chunks(
    mut artifact: JsPipelineArtifact,
    vendor: &BTreeMap<String, VendorMark>,
    references: &ArtifactIndexes,
    options: SwapVendorOptions<'_>,
) -> Result<SwapVendorChunksResult> {
    let ops: Vec<(&String, &VendorMark, &SwapMark)> = vendor
        .iter()
        .filter_map(|(chunk_path, mark)| match &mark.level {
            VendorLevel::Swap(swap) => Some((chunk_path, mark, swap)),
            _ => None,
        })
        .collect();
    let swap_chunk_names = ops
        .iter()
        .map(|(chunk_path, _mark, _swap)| {
            chunk_id_from_chunk_path(chunk_path, "swap_vendor_chunks")
        })
        .collect::<Result<BTreeSet<_>>>()?;
    let chunk_table = artifact.chunk_table.clone();
    let swap_chunk_ids: BTreeSet<ChunkId> = swap_chunk_names
        .iter()
        .filter_map(|name| chunk_table.get(name))
        .collect();
    let import_alignment_index =
        build_import_alignment_index(references, &swap_chunk_ids, &chunk_table);
    let jobs = ops
        .iter()
        .map(|(chunk_path, _mark, swap)| {
            let chunk_name = chunk_id_from_chunk_path(chunk_path, "swap_vendor_chunks")?;
            let chunk_id = chunk_table
                .get(&chunk_name)
                .with_context(|| format!("swap_vendor_chunks vendor entry {chunk_path} targets unknown chunk: {chunk_name}"))?;
            let entry_relative_file = get_chunk_entry_path(&artifact, chunk_id).with_context(|| {
            format!(
                "swap_vendor_chunks vendor entry {chunk_path} targets missing chunk (chunk_id={chunk_name})"
            )
        })?;
            let entry_ast = artifact
                .js_chunk(chunk_id)?
                .get_file(&entry_relative_file)
                .and_then(|file| file.ast())
                .with_context(|| {
                    format!("swap_vendor_chunks vendor chunk {chunk_name} is missing entry AST")
                })?;
            Ok(SwapVendorJob {
                chunk_path: (*chunk_path).clone(),
                chunk_name,
                entry_file: entry_relative_file,
                package: swap.package.clone(),
                version: swap.version.clone(),
                subpath: swap.subpath.clone(),
                wrapper_shape: swap.wrapper_shape,
                vendor_exports: collect_exported_names(&entry_ast.module, false),
            })
        })
        .collect::<Result<Vec<_>>>()?;
    let resolved = jobs
        .into_par_iter()
        .map(|job| resolve_vendor_swap(job, &import_alignment_index, &options))
        .collect::<Result<Vec<_>>>()?;
    let mut resolutions = BTreeMap::<String, VendorResolution>::new();
    for resolution in resolved {
        let chunk_id = chunk_table
            .get(&resolution.chunk_id)
            .context("swap_vendor_chunks resolution references unknown chunk")?;
        artifact.remove_chunk(chunk_id);
        resolutions.insert(resolution.chunk_path.clone(), resolution);
    }

    let chunk_count = artifact.list_chunk_ids().len();
    let removed: BTreeSet<String> = resolutions
        .keys()
        .map(|chunk_path| chunk_id_from_chunk_path(chunk_path, "swap_vendor_chunks"))
        .collect::<Result<BTreeSet<_>>>()?;
    artifact.root_manifest.counts.chunks = chunk_count;
    artifact
        .root_manifest
        .chunks
        .retain(|chunk| !removed.contains(&chunk.chunk_id));

    if options.write
        && let Some(output_manifest_path) = options.output_manifest_path
    {
        if let Some(parent) = output_manifest_path.parent() {
            fs::create_dir_all(parent)?;
        }
        #[derive(Serialize)]
        struct OnDiskResolutionManifest<'a> {
            resolutions: &'a BTreeMap<String, VendorResolution>,
        }
        fs::write(
            output_manifest_path,
            serde_json::to_string_pretty(&OnDiskResolutionManifest {
                resolutions: &resolutions,
            })? + "\n",
        )?;
    }

    let swapped = resolutions.len();
    Ok(SwapVendorChunksResult {
        artifact,
        manifest: VendorResolutionManifest {
            resolutions,
            counts: VendorResolutionCounts { swapped },
        },
    })
}

fn chunk_id_from_chunk_path(chunk_path: &str, stage: &str) -> Result<String> {
    if chunk_path.is_empty() {
        bail!("{stage}: empty chunk path");
    }
    let Some(chunk_id) = chunk_path.strip_suffix(".js") else {
        bail!("{stage}: chunk path must end in .js: {chunk_path}");
    };
    Ok(chunk_id.to_string())
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
    rewrites_by_target: BTreeMap<(String, String), usize>,
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
        let target_chunk_name = self.chunk_table.name(target_chunk_id).to_string();
        let Some(files) = self.mappings.get(&target_chunk_name) else {
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
                .entry((target_chunk_name.clone(), target_file.clone()))
                .or_insert(0) += 1;
        }
    }
}

#[derive(Debug, Clone)]
struct ImportAlignmentRecord {
    caller_chunk_id: String,
    caller_file: String,
    named_imports: Vec<String>,
}

fn build_import_alignment_index(
    references: &ArtifactIndexes,
    target_chunk_ids: &BTreeSet<ChunkId>,
    chunk_table: &ChunkTable,
) -> BTreeMap<String, Vec<ImportAlignmentRecord>> {
    let mut index = BTreeMap::<String, Vec<ImportAlignmentRecord>>::new();
    for target_chunk_id in target_chunk_ids {
        let target_chunk_name = chunk_table.name(*target_chunk_id).to_string();
        for import in references.manifest_imports_targeting_chunk(*target_chunk_id) {
            if import.named_imports.is_empty() {
                continue;
            }
            let caller_chunk_name = chunk_table.name(import.caller_chunk_id).to_string();
            index
                .entry(target_chunk_name.clone())
                .or_default()
                .push(ImportAlignmentRecord {
                    caller_chunk_id: caller_chunk_name,
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

fn collect_exported_names(module: &Module, include_default: bool) -> BTreeSet<String> {
    let mut names = BTreeSet::new();
    for item in &module.body {
        match item {
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(_))
            | ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(_)) => {
                if include_default {
                    names.insert("default".to_string());
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
                for name in declared_names(&export_decl.decl) {
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

fn declared_names(decl: &Decl) -> Vec<String> {
    match decl {
        Decl::Fn(function) => vec![function.ident.sym.to_string()],
        Decl::Class(class) => vec![class.ident.sym.to_string()],
        Decl::Var(var) => var
            .decls
            .iter()
            .flat_map(|decl| binding_names(&decl.name))
            .collect(),
        _ => Vec::new(),
    }
}

fn binding_names(pattern: &Pat) -> Vec<String> {
    match pattern {
        Pat::Ident(ident) => vec![ident.id.sym.to_string()],
        Pat::Rest(rest) => binding_names(&rest.arg),
        Pat::Assign(assign) => binding_names(&assign.left),
        Pat::Array(array) => array
            .elems
            .iter()
            .flatten()
            .flat_map(binding_names)
            .collect(),
        Pat::Object(object) => object
            .props
            .iter()
            .flat_map(|prop| match prop {
                ObjectPatProp::KeyValue(key_value) => binding_names(&key_value.value),
                ObjectPatProp::Assign(assign) => vec![assign.key.id.sym.to_string()],
                ObjectPatProp::Rest(rest) => binding_names(&rest.arg),
            })
            .collect(),
        _ => Vec::new(),
    }
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

fn generate_named_from_default_wrapper(
    upstream_ast: &ParsedJsModule,
    named_exports: &BTreeSet<String>,
) -> Result<String> {
    let mut body = Vec::new();
    let default_local = Ident::new_no_ctxt("_d".into(), DUMMY_SP);
    for item in &upstream_ast.module.body {
        match item {
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(default_expr)) => {
                body.push(ModuleItem::Stmt(Stmt::Decl(Decl::Var(Box::new(VarDecl {
                    span: DUMMY_SP,
                    ctxt: SyntaxContext::empty(),
                    kind: VarDeclKind::Const,
                    declare: false,
                    decls: vec![VarDeclarator {
                        span: DUMMY_SP,
                        name: Pat::Ident(BindingIdent {
                            id: default_local.clone(),
                            type_ann: None,
                        }),
                        init: Some(default_expr.expr.clone()),
                        definite: false,
                    }],
                })))));
            }
            _ => body.push(item.clone()),
        }
    }
    body.push(export_default_ident("_d"));
    for name in named_exports {
        body.push(export_const_member(name, "_d", name));
    }
    emit_js_module(
        &ParsedJsModule {
            cm: upstream_ast.cm.clone(),
            module: Module {
                span: DUMMY_SP,
                body,
                shebang: None,
            },
        },
        &[],
    )
}

fn generate_named_from_json_default_wrapper(
    upstream_json: &Value,
    named_exports: &BTreeSet<String>,
) -> String {
    let body = serde_json::to_string_pretty(upstream_json).unwrap_or_else(|_| "{}".to_string());
    let named = named_exports
        .iter()
        .map(|name| format!("export const {name} = _d.{name};"))
        .collect::<Vec<_>>()
        .join("\n");
    format!("const _d = {body};\nexport default _d;\n{named}\n")
}

fn generate_named_from_module_default_wrapper(
    upstream_ast: &ParsedJsModule,
    vendor_exports: &BTreeSet<String>,
    chunk_path: &str,
) -> Result<String> {
    let default_local_name = "__vendor_default__";
    let mut found_default = false;
    let mut deferred_default_alias: Option<String> = None;
    let mut body = Vec::new();
    for item in &upstream_ast.module.body {
        match item {
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(default_expr)) => {
                found_default = true;
                body.push(ModuleItem::Stmt(Stmt::Decl(Decl::Var(Box::new(VarDecl {
                    span: DUMMY_SP,
                    ctxt: SyntaxContext::empty(),
                    kind: VarDeclKind::Const,
                    declare: false,
                    decls: vec![VarDeclarator {
                        span: DUMMY_SP,
                        name: Pat::Ident(BindingIdent {
                            id: Ident::new_no_ctxt(default_local_name.into(), DUMMY_SP),
                            type_ann: None,
                        }),
                        init: Some(default_expr.expr.clone()),
                        definite: false,
                    }],
                })))));
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(default_decl)) => {
                found_default = true;
                match &default_decl.decl {
                    DefaultDecl::Fn(function) => {
                        if let Some(ident) = &function.ident {
                            body.push(ModuleItem::Stmt(Stmt::Decl(Decl::Fn(FnDecl {
                                ident: ident.clone(),
                                declare: false,
                                function: function.function.clone(),
                            }))));
                            body.push(const_alias(default_local_name, ident.sym.as_ref()));
                        } else {
                            // Anonymous `export default function () { ... }`
                            // collapses to `const __vendor_default__ = function () { ... };`.
                            body.push(const_init_with_expr(
                                default_local_name,
                                Expr::Fn(FnExpr {
                                    ident: None,
                                    function: function.function.clone(),
                                }),
                            ));
                        }
                    }
                    DefaultDecl::Class(class) => {
                        if let Some(ident) = &class.ident {
                            body.push(ModuleItem::Stmt(Stmt::Decl(Decl::Class(ClassDecl {
                                ident: ident.clone(),
                                declare: false,
                                class: class.class.clone(),
                            }))));
                            body.push(const_alias(default_local_name, ident.sym.as_ref()));
                        } else {
                            // Anonymous `export default class { ... }` collapses
                            // to `const __vendor_default__ = class { ... };`.
                            body.push(const_init_with_expr(
                                default_local_name,
                                Expr::Class(ClassExpr {
                                    ident: None,
                                    class: class.class.clone(),
                                }),
                            ));
                        }
                    }
                    DefaultDecl::TsInterfaceDecl(_) => {}
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named_decl)) => {
                if named_decl.src.is_some() {
                    body.push(item.clone());
                    continue;
                }
                let mut remaining = Vec::with_capacity(named_decl.specifiers.len());
                for specifier in &named_decl.specifiers {
                    let ExportSpecifier::Named(named_specifier) = specifier else {
                        remaining.push(specifier.clone());
                        continue;
                    };
                    let exported_name = named_specifier
                        .exported
                        .as_ref()
                        .map(module_export_name)
                        .unwrap_or_else(|| module_export_name(&named_specifier.orig));
                    if exported_name != "default" {
                        remaining.push(specifier.clone());
                        continue;
                    }
                    let ModuleExportName::Ident(local) = &named_specifier.orig else {
                        bail!(
                            "swap_vendor_chunks vendor entry {chunk_path} named-from-module-default: \"export {{ ... as default }}\" must alias a local identifier"
                        );
                    };
                    if deferred_default_alias.is_some() {
                        bail!(
                            "swap_vendor_chunks vendor entry {chunk_path} named-from-module-default: upstream declares more than one default export"
                        );
                    }
                    found_default = true;
                    // Defer the `const __vendor_default__ = <local>;` emission
                    // to the end of the body. ESM allows `export { lib as default };
                    // const lib = ...;`, so emitting the alias at the original
                    // export position would TDZ on `lib` if the export sits before
                    // the local declaration.
                    deferred_default_alias = Some(local.sym.to_string());
                }
                if !remaining.is_empty() {
                    let mut kept = named_decl.clone();
                    kept.specifiers = remaining;
                    body.push(ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(kept)));
                }
            }
            _ => body.push(item.clone()),
        }
    }
    if let Some(local) = deferred_default_alias {
        body.push(const_alias(default_local_name, &local));
    }
    if !found_default {
        bail!(
            "swap_vendor_chunks vendor entry {chunk_path} named-from-module-default: upstream has no default export"
        );
    }
    body.push(export_default_ident(default_local_name));
    for name in vendor_exports {
        if name == "default" {
            continue;
        }
        body.push(export_const_ident(name, default_local_name));
    }
    emit_js_module(
        &ParsedJsModule {
            cm: upstream_ast.cm.clone(),
            module: Module {
                span: DUMMY_SP,
                body,
                shebang: None,
            },
        },
        &[],
    )
}

fn write_wrapper_if_requested(
    write: bool,
    output_wrapper_dir: Option<&Path>,
    chunk_id: &str,
    entry_file: &str,
    source: &str,
) -> Result<Option<PathBuf>> {
    let Some(output_wrapper_dir) = output_wrapper_dir else {
        return Ok(None);
    };
    let wrapper_abs_path = output_wrapper_dir
        .join(path_from_module_path(chunk_id))
        .join(path_from_module_path(entry_file));
    if write {
        if let Some(parent) = wrapper_abs_path.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::write(&wrapper_abs_path, source)?;
    }
    Ok(Some(wrapper_abs_path))
}

fn set_diff(left: &BTreeSet<String>, right: &BTreeSet<String>) -> BTreeSet<String> {
    left.difference(right).cloned().collect()
}

fn export_default_ident(name: &str) -> ModuleItem {
    ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(ExportDefaultExpr {
        span: DUMMY_SP,
        expr: Box::new(Expr::Ident(Ident::new_no_ctxt(name.into(), DUMMY_SP))),
    }))
}

fn export_const_member(export_name: &str, object_name: &str, property_name: &str) -> ModuleItem {
    ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
        span: DUMMY_SP,
        decl: Decl::Var(Box::new(VarDecl {
            span: DUMMY_SP,
            ctxt: SyntaxContext::empty(),
            kind: VarDeclKind::Const,
            declare: false,
            decls: vec![VarDeclarator {
                span: DUMMY_SP,
                name: Pat::Ident(BindingIdent {
                    id: Ident::new_no_ctxt(export_name.into(), DUMMY_SP),
                    type_ann: None,
                }),
                init: Some(Box::new(Expr::Member(MemberExpr {
                    span: DUMMY_SP,
                    obj: Box::new(Expr::Ident(Ident::new_no_ctxt(
                        object_name.into(),
                        DUMMY_SP,
                    ))),
                    prop: MemberProp::Ident(IdentName::new(property_name.into(), DUMMY_SP)),
                }))),
                definite: false,
            }],
        })),
    }))
}

fn export_const_ident(export_name: &str, local_name: &str) -> ModuleItem {
    ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
        span: DUMMY_SP,
        decl: Decl::Var(Box::new(VarDecl {
            span: DUMMY_SP,
            ctxt: SyntaxContext::empty(),
            kind: VarDeclKind::Const,
            declare: false,
            decls: vec![VarDeclarator {
                span: DUMMY_SP,
                name: Pat::Ident(BindingIdent {
                    id: Ident::new_no_ctxt(export_name.into(), DUMMY_SP),
                    type_ann: None,
                }),
                init: Some(Box::new(Expr::Ident(Ident::new_no_ctxt(
                    local_name.into(),
                    DUMMY_SP,
                )))),
                definite: false,
            }],
        })),
    }))
}

fn const_alias(alias: &str, target: &str) -> ModuleItem {
    const_init_with_expr(
        alias,
        Expr::Ident(Ident::new_no_ctxt(target.into(), DUMMY_SP)),
    )
}

fn const_init_with_expr(alias: &str, init: Expr) -> ModuleItem {
    ModuleItem::Stmt(Stmt::Decl(Decl::Var(Box::new(VarDecl {
        span: DUMMY_SP,
        ctxt: SyntaxContext::empty(),
        kind: VarDeclKind::Const,
        declare: false,
        decls: vec![VarDeclarator {
            span: DUMMY_SP,
            name: Pat::Ident(BindingIdent {
                id: Ident::new_no_ctxt(alias.into(), DUMMY_SP),
                type_ann: None,
            }),
            init: Some(Box::new(init)),
            definite: false,
        }],
    }))))
}

fn module_export_name(name: &ModuleExportName) -> String {
    name.atom().to_string()
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
        ChunkArtifact, ChunkManifest, ChunkMetadata, FileMetadata, FileRole, JsChunk, JsFile,
    };

    #[test]
    fn rename_vendor_exports_rewrites_multiple_vendor_targets_in_one_call() {
        let mut artifact = JsPipelineArtifact::default();
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
        let result = rename_vendor_exports(artifact, &vendor, &references).unwrap();
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
    }

    fn insert_chunk(artifact: &mut JsPipelineArtifact, chunk_id: &str, source: &str) {
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
                    metadata: FileMetadata {
                        chunk_id: Some(chunk_id.to_string()),
                        chunk_file: Some(entry_file.clone()),
                        role: Some(FileRole::Entry),
                        source_path: Some(format!("{chunk_id}.js")),
                        ..Default::default()
                    },
                }],
                metadata: ChunkMetadata {
                    source_path: Some(format!("{chunk_id}.js")),
                    module_extraction_state: None,
                },
            },
            manifest: ChunkManifest {
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
                logical_modules: None,
                selected_module_lowerings: None,
                output_metrics: None,
            },
        });
    }

    fn named_imports(
        artifact: &JsPipelineArtifact,
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
}
