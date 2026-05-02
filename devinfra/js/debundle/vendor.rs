use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use serde::Serialize;
use serde_json::{Map, Value, json};
use swc_common::{DUMMY_SP, SyntaxContext};
use swc_ecma_ast::*;
use swc_ecma_visit::{VisitMut, VisitMutWith};

use artifact::{
    JsPipelineArtifact, get_chunk_entry_path, list_chunk_file_paths,
    resolve_artifact_import_reference, resolve_artifact_source_import_reference, split_posix_path,
};
use js_ast::{ParsedJsModule, emit_js_module, parse_js_module, str_value};

#[derive(Debug, Clone, Serialize)]
pub struct VendorAnnotationsManifest {
    pub kind: &'static str,
    pub counts: VendorAnnotationCounts,
    pub annotations: Vec<Value>,
}

#[derive(Debug, Clone, Serialize)]
pub struct VendorAnnotationCounts {
    pub annotations: usize,
    pub considered: usize,
    pub applied: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct RenameVendorExportsManifest {
    pub kind: &'static str,
    pub counts: RenameVendorExportsCounts,
    pub details: Vec<RenameVendorExportsDetail>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RenameVendorExportsCounts {
    pub considered: usize,
    pub chunks_with_mapping: usize,
    pub rewrites: usize,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RenameVendorExportsDetail {
    pub op_id: String,
    pub chunk_id: String,
    pub mapping_size: usize,
    pub rewrites: usize,
    pub callers: Vec<RenameVendorExportsCaller>,
}

#[derive(Debug, Clone, Serialize)]
pub struct RenameVendorExportsCaller {
    pub file: String,
    pub rewrites: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct VendorResolutionManifest {
    pub kind: &'static str,
    pub resolutions: BTreeMap<String, Value>,
    pub counts: VendorResolutionCounts,
}

#[derive(Debug, Clone, Serialize)]
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
    artifact: &mut JsPipelineArtifact,
    operations: &[Value],
) -> Result<VendorAnnotationsManifest> {
    let ops = mark_vendor_ops(operations)
        .into_iter()
        .filter(|op| value_str(op, "operation") == Some("mark_vendor"))
        .collect::<Vec<_>>();
    let mut seen_chunk_paths = BTreeMap::<String, String>::new();
    artifact.vendor_annotations.clear();
    let mut summaries = Vec::new();

    for op in &ops {
        validate_mark_vendor_op(op)?;
        let op_id = required_str(op, "id")?;
        let chunk_path = required_str(op, "chunkPath")?;
        let chunk_id = chunk_id_from_chunk_path(chunk_path, op_id, "mark_vendor")?;
        if get_chunk_entry_path(artifact, &chunk_id).is_none() {
            bail!(
                "mark_vendor operation {op_id} targets missing chunk: chunkPath={chunk_path} (chunkId={chunk_id})"
            );
        }
        if let Some(prior_id) = seen_chunk_paths.insert(chunk_path.to_string(), op_id.to_string()) {
            bail!(
                "mark_vendor operations {prior_id} and {op_id} both target chunkPath {chunk_path}"
            );
        }

        let mut annotation = Map::new();
        insert_required(&mut annotation, op, "id");
        insert_required(&mut annotation, op, "level");
        insert_required(&mut annotation, op, "chunkPath");
        annotation.insert("chunkId".to_string(), Value::String(chunk_id.clone()));
        insert_required(&mut annotation, op, "identity");
        insert_required(&mut annotation, op, "evidence");
        for key in [
            "upstreamFamily",
            "version",
            "confidence",
            "notes",
            "package",
            "subpath",
            "exportShape",
            "fingerprint",
        ] {
            insert_optional(&mut annotation, op, key);
        }
        annotation.insert(
            "role".to_string(),
            Value::String(value_str(op, "role").unwrap_or("module").to_string()),
        );
        artifact
            .vendor_annotations
            .insert(chunk_id.clone(), Value::Object(annotation.clone()));

        let mut summary = Map::new();
        for key in ["id", "chunkPath", "identity", "level"] {
            insert_required(&mut summary, op, key);
        }
        summary.insert("chunkId".to_string(), Value::String(chunk_id));
        for key in [
            "upstreamFamily",
            "version",
            "confidence",
            "package",
            "subpath",
        ] {
            insert_optional(&mut summary, op, key);
        }
        summary.insert(
            "role".to_string(),
            Value::String(value_str(op, "role").unwrap_or("module").to_string()),
        );
        summaries.push(Value::Object(summary));
    }

    Ok(VendorAnnotationsManifest {
        kind: "js.vendor_annotations_manifest",
        counts: VendorAnnotationCounts {
            annotations: artifact.vendor_annotations.len(),
            considered: operations.len(),
            applied: ops.len(),
        },
        annotations: summaries,
    })
}

pub fn rename_vendor_exports(
    artifact: &mut JsPipelineArtifact,
    operations: &[Value],
) -> Result<RenameVendorExportsManifest> {
    let ops = mark_vendor_ops(operations)
        .into_iter()
        .filter(|op| matches!(value_str(op, "level"), Some("boundary-rename" | "swap")))
        .collect::<Vec<_>>();
    let mut total_rewrites = 0usize;
    let mut chunks_with_mapping = 0usize;
    let mut details = Vec::new();

    for op in &ops {
        let op_id = required_str(op, "id")?;
        let chunk_path = required_str(op, "chunkPath")?;
        let chunk_id = chunk_id_from_chunk_path(chunk_path, op_id, "renameVendorExports")?;
        let vendor_entry_relative_file = get_chunk_entry_path(artifact, &chunk_id).with_context(|| {
            format!(
                "renameVendorExports operation {op_id} targets missing chunk: chunkPath={chunk_path} (chunkId={chunk_id})"
            )
        })?;
        let mapping = {
            let vendor_ast = artifact
                .chunks
                .get(&chunk_id)
                .and_then(|chunk| chunk.files.get(&vendor_entry_relative_file))
                .and_then(|file| file.ast.as_ref())
                .with_context(|| {
                    format!(
                        "renameVendorExports operation {op_id} vendor chunk {chunk_id} is missing entry AST"
                    )
                })?;
            collect_boundary_mapping(&vendor_ast.module)
        };
        if mapping.is_empty() {
            details.push(RenameVendorExportsDetail {
                op_id: op_id.to_string(),
                chunk_id,
                mapping_size: 0,
                rewrites: 0,
                callers: Vec::new(),
            });
            continue;
        }
        chunks_with_mapping += 1;

        let mut caller_counts = BTreeMap::<String, usize>::new();
        let mut chunk_rewrites = 0usize;
        let file_keys = artifact.list_js_file_keys();
        for (other_chunk_id, file_path) in file_keys {
            if other_chunk_id == chunk_id {
                continue;
            }
            let has_ast = artifact
                .chunks
                .get(&other_chunk_id)
                .and_then(|chunk| chunk.files.get(&file_path))
                .and_then(|file| file.ast.as_ref())
                .is_some();
            if !has_ast {
                continue;
            }
            let mut ast = artifact
                .chunks
                .get_mut(&other_chunk_id)
                .and_then(|chunk| chunk.files.get_mut(&file_path))
                .and_then(|file| file.ast.take())
                .with_context(|| format!("missing AST for {other_chunk_id}/{file_path}"))?;
            let mut rewriter = VendorImportRenamer {
                artifact,
                caller_chunk_id: other_chunk_id.clone(),
                caller_file: file_path.clone(),
                target_chunk_id: chunk_id.clone(),
                target_entry_file: vendor_entry_relative_file.clone(),
                mapping: &mapping,
                rewrites: 0,
            };
            ast.module.visit_mut_with(&mut rewriter);
            let rewrites = rewriter.rewrites;
            drop(rewriter);
            artifact
                .chunks
                .get_mut(&other_chunk_id)
                .and_then(|chunk| chunk.files.get_mut(&file_path))
                .context("missing file while restoring AST")?
                .ast = Some(ast);
            if rewrites > 0 {
                chunk_rewrites += rewrites;
                caller_counts.insert(format!("{other_chunk_id}/{file_path}"), rewrites);
            }
        }

        total_rewrites += chunk_rewrites;
        details.push(RenameVendorExportsDetail {
            op_id: op_id.to_string(),
            chunk_id,
            mapping_size: mapping.len(),
            rewrites: chunk_rewrites,
            callers: caller_counts
                .into_iter()
                .map(|(file, rewrites)| RenameVendorExportsCaller { file, rewrites })
                .collect(),
        });
    }

    Ok(RenameVendorExportsManifest {
        kind: "js.rename_vendor_exports_manifest",
        counts: RenameVendorExportsCounts {
            considered: ops.len(),
            chunks_with_mapping,
            rewrites: total_rewrites,
        },
        details,
    })
}

pub fn swap_vendor_chunks(
    artifact: &mut JsPipelineArtifact,
    operations: &[Value],
    options: SwapVendorOptions<'_>,
) -> Result<VendorResolutionManifest> {
    let ops = mark_vendor_ops(operations)
        .into_iter()
        .filter(|op| value_str(op, "level") == Some("swap"))
        .collect::<Vec<_>>();
    let import_alignment_index = build_import_alignment_index(artifact)?;
    let mut resolutions = BTreeMap::<String, Value>::new();

    for op in &ops {
        let op_id = required_str(op, "id")?;
        let chunk_path = required_str(op, "chunkPath")?;
        let chunk_id = chunk_id_from_chunk_path(chunk_path, op_id, "swapVendorChunks")?;
        let entry_relative_file = get_chunk_entry_path(artifact, &chunk_id).with_context(|| {
            format!(
                "swapVendorChunks operation {op_id} targets missing chunk: chunkPath={chunk_path} (chunkId={chunk_id})"
            )
        })?;
        let entry_ast = artifact
            .chunks
            .get(&chunk_id)
            .and_then(|chunk| chunk.files.get(&entry_relative_file))
            .and_then(|file| file.ast.as_ref())
            .with_context(|| {
                format!("swapVendorChunks operation {op_id} vendor chunk {chunk_id} is missing entry AST")
            })?;
        let package = required_str(op, "package")?;
        let version = required_str(op, "version")?;
        let subpath = required_str(op, "subpath")?;
        let installed =
            read_installed_package_metadata(package, options.package_roots, options.packages_root)
                .with_context(|| format!("reading metadata for package {package}"))?;
        let installed_version = installed
            .get("version")
            .and_then(Value::as_str)
            .context("package metadata missing version")?;
        if installed_version != version {
            bail!(
                "swapVendorChunks operation {op_id} version mismatch for {package}: op={version}, installed={installed_version}"
            );
        }
        let upstream_path = resolve_package_subpath(
            package,
            subpath,
            options.package_roots,
            options.packages_root,
        )?;
        let upstream_code = fs::read_to_string(&upstream_path)
            .with_context(|| format!("reading {}", upstream_path.display()))?;
        let vendor_exports = collect_exported_names(&entry_ast.module, false);
        let mut generated_wrapper_path = None::<String>;

        match value_str(op, "wrapperShape") {
            Some("named-from-default") => {
                let upstream_ast =
                    parse_js_module(&upstream_path.display().to_string(), &upstream_code)?;
                let object_keys = collect_default_export_object_keys(&upstream_ast.module, op_id)?;
                let non_default_exports = vendor_exports
                    .iter()
                    .filter(|name| name.as_str() != "default")
                    .cloned()
                    .collect::<BTreeSet<_>>();
                let missing = set_diff(&non_default_exports, &object_keys);
                if !missing.is_empty() {
                    bail!(
                        "swapVendorChunks operation {op_id} named-from-default wrapper shape mismatch for {package}@{version}: vendor named exports missing from upstream default object keys=[{}]",
                        missing.into_iter().collect::<Vec<_>>().join(",")
                    );
                }
                let wrapper =
                    generate_named_from_default_wrapper(&upstream_ast, &non_default_exports)?;
                generated_wrapper_path = write_wrapper_if_requested(
                    options.write,
                    options.output_wrapper_dir.as_deref(),
                    &chunk_id,
                    &entry_relative_file,
                    &wrapper,
                )?;
            }
            Some("named-from-json-default") => {
                let upstream_json = serde_json::from_str::<Value>(&upstream_code).with_context(|| {
                    format!("swapVendorChunks operation {op_id} named-from-json-default: upstream JSON parse failed")
                })?;
                let object = upstream_json
                    .as_object()
                    .context("named-from-json-default upstream JSON must be an object")?;
                let object_keys = object.keys().cloned().collect::<BTreeSet<_>>();
                let non_default_exports = vendor_exports
                    .iter()
                    .filter(|name| name.as_str() != "default")
                    .cloned()
                    .collect::<BTreeSet<_>>();
                let missing = set_diff(&non_default_exports, &object_keys);
                if !missing.is_empty() {
                    bail!(
                        "swapVendorChunks operation {op_id} named-from-json-default wrapper shape mismatch for {package}@{version}: vendor named exports missing from upstream JSON keys=[{}]",
                        missing.into_iter().collect::<Vec<_>>().join(",")
                    );
                }
                let wrapper =
                    generate_named_from_json_default_wrapper(&upstream_json, &non_default_exports);
                generated_wrapper_path = write_wrapper_if_requested(
                    options.write,
                    options.output_wrapper_dir.as_deref(),
                    &chunk_id,
                    &entry_relative_file,
                    &wrapper,
                )?;
            }
            Some("named-from-module-default") => {
                let upstream_ast =
                    parse_js_module(&upstream_path.display().to_string(), &upstream_code)?;
                let wrapper = generate_named_from_module_default_wrapper(
                    &upstream_ast,
                    &vendor_exports,
                    op_id,
                )?;
                generated_wrapper_path = write_wrapper_if_requested(
                    options.write,
                    options.output_wrapper_dir.as_deref(),
                    &chunk_id,
                    &entry_relative_file,
                    &wrapper,
                )?;
            }
            Some(other) => {
                bail!("swapVendorChunks operation {op_id} has unsupported wrapperShape {other}")
            }
            None => {
                let upstream_ast =
                    parse_js_module(&upstream_path.display().to_string(), &upstream_code)?;
                let upstream_exports = collect_exported_names(&upstream_ast.module, true);
                let missing = set_diff(&vendor_exports, &upstream_exports);
                if !missing.is_empty() {
                    bail!(
                        "swapVendorChunks operation {op_id} export shape mismatch for {package}@{version}: vendor exports not found upstream=[{}]",
                        missing.into_iter().collect::<Vec<_>>().join(",")
                    );
                }
            }
        }

        for record in import_alignment_index.get(&chunk_id).into_iter().flatten() {
            for imported_name in &record.named_imports {
                if vendor_exports.contains(imported_name) {
                    continue;
                }
                bail!(
                    "swapVendorChunks operation {op_id} import alignment failed: caller={}/{} imports unknown specifier \"{}\" from vendor {chunk_id} (known: [{}])",
                    record.caller_chunk_id,
                    record.caller_file,
                    imported_name,
                    vendor_exports.iter().cloned().collect::<Vec<_>>().join(",")
                );
            }
        }

        artifact.chunks.remove(&chunk_id);
        artifact
            .chunk_order
            .retain(|candidate| candidate != &chunk_id);
        artifact.chunk_manifests.remove(&chunk_id);
        let mut swap_entry = Map::new();
        swap_entry.insert("package".to_string(), Value::String(package.to_string()));
        swap_entry.insert("version".to_string(), Value::String(version.to_string()));
        swap_entry.insert("subpath".to_string(), Value::String(subpath.to_string()));
        swap_entry.insert(
            "verification".to_string(),
            Value::String("structural".to_string()),
        );
        if let Some(wrapper_shape) = value_str(op, "wrapperShape") {
            swap_entry.insert(
                "wrapperShape".to_string(),
                Value::String(wrapper_shape.to_string()),
            );
        }
        if let Some(path) = &generated_wrapper_path {
            swap_entry.insert(
                "generatedWrapperPath".to_string(),
                Value::String(path.clone()),
            );
        }
        let mut annotation = artifact
            .vendor_annotations
            .get(&chunk_id)
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        annotation.insert("chunkId".to_string(), Value::String(chunk_id.clone()));
        annotation.insert(
            "chunkPath".to_string(),
            Value::String(chunk_path.to_string()),
        );
        annotation.insert("level".to_string(), Value::String("swap".to_string()));
        annotation
            .entry("id".to_string())
            .or_insert_with(|| Value::String(op_id.to_string()));
        annotation.insert("swap".to_string(), Value::Object(swap_entry.clone()));
        artifact
            .vendor_annotations
            .insert(chunk_id.clone(), Value::Object(annotation));

        let mut resolution = Map::new();
        resolution.insert("chunkId".to_string(), Value::String(chunk_id));
        resolution.insert(
            "chunkPath".to_string(),
            Value::String(chunk_path.to_string()),
        );
        resolution.insert("entryFile".to_string(), Value::String(entry_relative_file));
        resolution.insert("package".to_string(), Value::String(package.to_string()));
        resolution.insert("version".to_string(), Value::String(version.to_string()));
        resolution.insert("subpath".to_string(), Value::String(subpath.to_string()));
        if let Some(wrapper_shape) = value_str(op, "wrapperShape") {
            resolution.insert(
                "wrapperShape".to_string(),
                Value::String(wrapper_shape.to_string()),
            );
        }
        if let Some(path) = generated_wrapper_path {
            resolution.insert("generatedWrapperPath".to_string(), Value::String(path));
        }
        resolutions.insert(chunk_path.to_string(), Value::Object(resolution));
    }

    let chunk_count = artifact.list_chunk_ids().len();
    if let Some(root_manifest) = &mut artifact.root_manifest {
        let removed = resolutions
            .keys()
            .map(|chunk_path| chunk_id_from_chunk_path(chunk_path, "manifest", "swapVendorChunks"))
            .collect::<Result<BTreeSet<_>>>()?;
        root_manifest.counts.chunks = chunk_count;
        root_manifest
            .chunks
            .retain(|chunk| !removed.contains(&chunk.chunk_id));
    }

    if options.write
        && let Some(output_manifest_path) = options.output_manifest_path
    {
        if let Some(parent) = output_manifest_path.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::write(
            output_manifest_path,
            serde_json::to_string_pretty(&json!({
                "kind": "js.vendor_resolution_manifest",
                "resolutions": resolutions,
            }))? + "\n",
        )?;
    }

    let swapped = resolutions.len();
    Ok(VendorResolutionManifest {
        kind: "js.vendor_resolution_manifest",
        resolutions,
        counts: VendorResolutionCounts { swapped },
    })
}

fn mark_vendor_ops(operations: &[Value]) -> Vec<&Value> {
    operations
        .iter()
        .filter(|op| value_str(op, "operation") == Some("mark_vendor"))
        .collect()
}

fn validate_mark_vendor_op(op: &Value) -> Result<()> {
    let op_id = value_str(op, "id").unwrap_or("<unknown>");
    for field in [
        "id",
        "operation",
        "level",
        "chunkPath",
        "identity",
        "evidence",
    ] {
        if value_is_missing(op.get(field)) {
            bail!("mark_vendor operation {op_id} is missing required field: {field}");
        }
    }
    if value_str(op, "operation") != Some("mark_vendor") {
        bail!(
            "mark_vendor operation {} has unexpected operation: {}",
            required_str(op, "id")?,
            value_str(op, "operation").unwrap_or("<missing>")
        );
    }
    if !matches!(
        value_str(op, "level"),
        Some("suppress" | "boundary-rename" | "swap")
    ) {
        bail!(
            "mark_vendor operation {} has unknown level: {}",
            required_str(op, "id")?,
            value_str(op, "level").unwrap_or("<missing>")
        );
    }
    if let Some(role) = value_str(op, "role")
        && role != "module"
        && role != "worker"
    {
        bail!(
            "mark_vendor operation {} has invalid role: {role}",
            required_str(op, "id")?
        );
    }
    if value_str(op, "level") == Some("swap") {
        for field in ["package", "version", "subpath"] {
            if value_is_missing(op.get(field)) {
                bail!(
                    "mark_vendor operation {} level \"swap\" is missing required field: {field}",
                    required_str(op, "id")?
                );
            }
        }
    }
    if let Some(export_shape) = op.get("exportShape")
        && !export_shape.is_object()
    {
        bail!(
            "mark_vendor operation {} exportShape must be an object",
            required_str(op, "id")?
        );
    }
    if let Some(fp) = op.get("fingerprint")
        && (!fp.is_object()
            || fp.get("algorithm").and_then(Value::as_str).is_none()
            || fp.get("hash").and_then(Value::as_str).is_none())
    {
        bail!(
            "mark_vendor operation {} fingerprint must have algorithm and hash strings",
            required_str(op, "id")?
        );
    }
    if op
        .get("evidence")
        .and_then(Value::as_array)
        .is_none_or(|evidence| evidence.is_empty())
    {
        bail!(
            "mark_vendor operation {} requires a non-empty evidence array",
            required_str(op, "id")?
        );
    }
    Ok(())
}

fn value_is_missing(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => true,
        Some(Value::String(text)) => text.is_empty(),
        _ => false,
    }
}

fn required_str<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    value
        .get(key)
        .and_then(Value::as_str)
        .filter(|text| !text.is_empty())
        .with_context(|| format!("missing required string field {key}"))
}

fn value_str<'a>(value: &'a Value, key: &str) -> Option<&'a str> {
    value.get(key).and_then(Value::as_str)
}

fn insert_required(target: &mut Map<String, Value>, source: &Value, key: &str) {
    if let Some(value) = source.get(key) {
        target.insert(key.to_string(), value.clone());
    }
}

fn insert_optional(target: &mut Map<String, Value>, source: &Value, key: &str) {
    if let Some(value) = source.get(key) {
        target.insert(key.to_string(), value.clone());
    }
}

fn chunk_id_from_chunk_path(chunk_path: &str, op_id: &str, stage: &str) -> Result<String> {
    if chunk_path.is_empty() {
        bail!("{stage} operation {op_id} has invalid chunkPath: {chunk_path}");
    }
    let Some(chunk_id) = chunk_path.strip_suffix(".js") else {
        bail!("{stage} operation {op_id} chunkPath must end in .js: {chunk_path}");
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
    artifact: &'a JsPipelineArtifact,
    caller_chunk_id: String,
    caller_file: String,
    target_chunk_id: String,
    target_entry_file: String,
    mapping: &'a BTreeMap<String, String>,
    rewrites: usize,
}

impl VisitMut for VendorImportRenamer<'_> {
    fn visit_mut_import_decl(&mut self, node: &mut ImportDecl) {
        let source = str_value(&node.src);
        let resolved = resolve_artifact_import_reference(
            self.artifact,
            &source,
            &self.caller_chunk_id,
            &self.caller_file,
        )
        .or_else(|| {
            resolve_artifact_source_import_reference(
                self.artifact,
                &source,
                &self.caller_chunk_id,
                &self.caller_file,
            )
            .ok()
            .flatten()
            .map(|(chunk_id, file, _)| (chunk_id, file))
        });
        if !matches!(resolved, Some((ref chunk_id, ref file)) if chunk_id == &self.target_chunk_id && file == &self.target_entry_file)
        {
            return;
        }
        for specifier in &mut node.specifiers {
            let ImportSpecifier::Named(named) = specifier else {
                continue;
            };
            let imported_name = named
                .imported
                .as_ref()
                .map(module_export_name)
                .unwrap_or_else(|| named.local.sym.to_string());
            let Some(mapped) = self.mapping.get(&imported_name) else {
                continue;
            };
            if mapped == &imported_name {
                continue;
            }
            named.imported = Some(ModuleExportName::Ident(Ident::new_no_ctxt(
                mapped.clone().into(),
                DUMMY_SP,
            )));
            self.rewrites += 1;
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
    artifact: &JsPipelineArtifact,
) -> Result<BTreeMap<String, Vec<ImportAlignmentRecord>>> {
    let mut index = BTreeMap::<String, Vec<ImportAlignmentRecord>>::new();
    for caller_chunk_id in artifact.list_chunk_ids() {
        let Some(chunk) = artifact.chunks.get(&caller_chunk_id) else {
            continue;
        };
        for caller_file in list_chunk_file_paths(chunk) {
            let Some(ast) = chunk
                .files
                .get(&caller_file)
                .and_then(|file| file.ast.as_ref())
            else {
                continue;
            };
            for item in &ast.module.body {
                let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item else {
                    continue;
                };
                let source = str_value(&import.src);
                let target_chunk_id =
                    resolve_import_to_chunk_id(artifact, &source, &caller_chunk_id, &caller_file)?;
                let Some(target_chunk_id) = target_chunk_id else {
                    continue;
                };
                let named_imports = import
                    .specifiers
                    .iter()
                    .filter_map(|specifier| match specifier {
                        ImportSpecifier::Named(named) => Some(
                            named
                                .imported
                                .as_ref()
                                .map(module_export_name)
                                .unwrap_or_else(|| named.local.sym.to_string()),
                        ),
                        _ => None,
                    })
                    .collect::<Vec<_>>();
                if named_imports.is_empty() {
                    continue;
                }
                index
                    .entry(target_chunk_id)
                    .or_default()
                    .push(ImportAlignmentRecord {
                        caller_chunk_id: caller_chunk_id.clone(),
                        caller_file: caller_file.clone(),
                        named_imports,
                    });
            }
        }
    }
    Ok(index)
}

fn resolve_import_to_chunk_id(
    artifact: &JsPipelineArtifact,
    source: &str,
    caller_chunk_id: &str,
    caller_file: &str,
) -> Result<Option<String>> {
    if let Some((chunk_id, _file)) =
        resolve_artifact_import_reference(artifact, source, caller_chunk_id, caller_file)
    {
        return Ok(Some(chunk_id));
    }
    Ok(
        resolve_artifact_source_import_reference(artifact, source, caller_chunk_id, caller_file)?
            .map(|(chunk_id, _file, _path)| chunk_id),
    )
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

fn collect_default_export_object_keys(module: &Module, op_id: &str) -> Result<BTreeSet<String>> {
    for item in &module.body {
        let ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(default_expr)) = item else {
            continue;
        };
        let Expr::Object(object) = &*default_expr.expr else {
            bail!(
                "swapVendorChunks operation {op_id} named-from-default: upstream default export is not an object literal"
            );
        };
        let mut keys = BTreeSet::new();
        for prop in &object.props {
            let PropOrSpread::Prop(prop) = prop else {
                continue;
            };
            let Prop::KeyValue(key_value) = &**prop else {
                continue;
            };
            if let Some(key) = prop_name(&key_value.key) {
                keys.insert(key);
            }
        }
        return Ok(keys);
    }
    bail!(
        "swapVendorChunks operation {op_id} named-from-default: upstream has no export default declaration"
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
    op_id: &str,
) -> Result<String> {
    let default_local_name = "__vendor_default__";
    let mut found_default = false;
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
                            bail!(
                                "swapVendorChunks operation {op_id} named-from-module-default: anonymous default function is unsupported by Rust AST wrapper"
                            );
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
                            bail!(
                                "swapVendorChunks operation {op_id} named-from-module-default: anonymous default class is unsupported by Rust AST wrapper"
                            );
                        }
                    }
                    DefaultDecl::TsInterfaceDecl(_) => {}
                }
            }
            _ => body.push(item.clone()),
        }
    }
    if !found_default {
        bail!(
            "swapVendorChunks operation {op_id} named-from-module-default: upstream has no default export"
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
) -> Result<Option<String>> {
    let wrapper_rel_path = format!("vendors/generated/{chunk_id}/{entry_file}");
    if write && let Some(output_wrapper_dir) = output_wrapper_dir {
        let wrapper_abs_path = output_wrapper_dir
            .join(split_posix_path(chunk_id))
            .join(split_posix_path(entry_file));
        if let Some(parent) = wrapper_abs_path.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::write(wrapper_abs_path, source)?;
    }
    Ok(Some(wrapper_rel_path))
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
            init: Some(Box::new(Expr::Ident(Ident::new_no_ctxt(
                target.into(),
                DUMMY_SP,
            )))),
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
