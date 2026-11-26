use super::*;

/// Canonical input for apply runs that do not export replayable graphs.
/// Retain authored paths for report locations and every report/validation note.
/// Only module-level `note` is unused by the transform pipeline.
#[derive(Debug)]
pub(super) struct ApplyProjection {
    pub(super) source: TransformSpecSource,
    pub(super) spec: TransformSpec,
    pub(super) module_roots_by_chunk: BTreeMap<String, Vec<PathBuf>>,
    pub(super) rooted_paths: BTreeSet<ApplyPath>,
}

pub(super) const APPLY_OUTPUT_ROOT: &str = "__debundle_apply_output_root__";

/// Tree compilation creates these paths from `out_root`. Keep their suffixes
/// in the projection and bind them to the consuming target's output root.
/// Authored source paths remain subject to the ordinary projection-root mapping.
pub(super) fn map_tree_output_paths(
    spec: &mut TransformSpec,
    from: &Path,
    to: &Path,
) -> Result<()> {
    fn replace_root(path: &Path, from: &Path, to: &Path) -> Result<PathBuf> {
        let suffix = path.strip_prefix(from).with_context(|| {
            format!(
                "tree output {} is outside its output root {}",
                path.display(),
                from.display()
            )
        })?;
        Ok(if suffix.as_os_str().is_empty() {
            to.to_path_buf()
        } else {
            to.join(suffix)
        })
    }
    let map = |path: &mut Option<PathBuf>| -> Result<()> {
        if let Some(path) = path {
            *path = replace_root(path, from, to)?;
        }
        Ok(())
    };
    map(&mut spec.swap_vendor_chunks.output_manifest_path)?;
    map(&mut spec.swap_vendor_chunks.output_wrapper_dir)?;
    map(&mut spec.materialize_logical_modules.report_out_dir)?;
    if let Some(write) = &mut spec.write_js_tree {
        write.out_dir = replace_root(&write.out_dir, from, to)?;
    }
    if let Some(browser) = &mut spec.emit_browser_harness {
        browser.out_dir = replace_root(&browser.out_dir, from, to)?;
    }
    Ok(())
}

#[cfg(test)]
mod apply_projection_output_tests {
    use super::*;

    #[test]
    fn tree_output_binding_moves_only_generated_paths() {
        let mut spec: TransformSpec = serde_json::from_value(serde_json::json!({
            "inputs": {"input_root": "snapshot", "js_list_path": "js-files.txt"},
            "swap_vendor_chunks": {
                "output_manifest_path": "first/reports/vendor_swaps.json",
                "output_wrapper_dir": "first/app/vendors/generated"
            },
            "materialize_logical_modules": {"report_out_dir": "first/reports/tree"},
            "write_js_tree": {"out_dir": "first"},
            "emit_browser_harness": {
                "asset_summary_path": "snapshot/assets.json",
                "out_dir": "first",
                "snapshot_root": "snapshot"
            }
        }))
        .unwrap();

        map_tree_output_paths(&mut spec, Path::new("first"), Path::new("second")).unwrap();
        assert_eq!(
            spec.swap_vendor_chunks.output_manifest_path.as_deref(),
            Some(Path::new("second/reports/vendor_swaps.json"))
        );
        assert_eq!(
            spec.swap_vendor_chunks.output_wrapper_dir.as_deref(),
            Some(Path::new("second/app/vendors/generated"))
        );
        assert_eq!(
            spec.materialize_logical_modules.report_out_dir.as_deref(),
            Some(Path::new("second/reports/tree"))
        );
        assert_eq!(spec.write_js_tree.unwrap().out_dir, Path::new("second"));
        let browser = spec.emit_browser_harness.unwrap();
        assert_eq!(browser.out_dir, Path::new("second"));
        assert_eq!(
            browser.asset_summary_path,
            Path::new("snapshot/assets.json")
        );
        assert_eq!(browser.snapshot_root, Path::new("snapshot"));
        assert_eq!(spec.inputs.input_root, Path::new("snapshot"));
    }
}

/// Which spec paths were absolute beneath the producer root. Relative authored
/// paths keep their exact spelling for native analysis compatibility.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub(super) enum ApplyPath {
    InputRoot,
    JsList,
    EmbeddedLoaders,
    VendorBundle(String),
    VendorManifest,
    VendorWrappers,
    Reports,
    JsOutput,
    BrowserAssets,
    BrowserOutput,
    BrowserSnapshot,
}

pub fn project_apply(cli: &TransformRequest, root: &Path, out: &Path) -> Result<()> {
    let (spec, roots) = load_transform_spec_with_roots(&cli.spec_source)?;
    project_apply_from_spec(cli, root, out, spec, roots)
}

pub(super) fn project_apply_from_spec(
    cli: &TransformRequest,
    root: &Path,
    out: &Path,
    mut spec: TransformSpec,
    module_roots_by_chunk: BTreeMap<String, Vec<PathBuf>>,
) -> Result<()> {
    validate_transform_spec(&spec)?;
    if let TransformSpecSource::Tree(tree) = &cli.spec_source {
        map_tree_output_paths(&mut spec, &tree.out_root, Path::new(APPLY_OUTPUT_ROOT))?;
    }
    for modules in spec.logical_modules.values_mut() {
        for module in modules.values_mut() {
            module.note = None;
        }
    }
    let root = std::path::absolute(root)?;
    let source = projection_source(&cli.spec_source, &root)?;
    let mut rooted_paths = BTreeSet::new();
    map_apply_spec_paths(&mut spec, &mut |field, path| {
        if let Ok(relative) = path.strip_prefix(&root) {
            rooted_paths.insert(field);
            Ok(relative.to_path_buf())
        } else {
            Ok(path.to_path_buf())
        }
    })?;
    fs::write(
        out,
        ApplyProjection {
            source,
            spec,
            module_roots_by_chunk,
            rooted_paths,
        }
        .encode(),
    )
    .with_context(|| format!("write apply projection {}", out.display()))
}

pub(super) fn projection_source(
    source: &TransformSpecSource,
    root: &Path,
) -> Result<TransformSpecSource> {
    let relative = |path: &Path| -> Result<PathBuf> {
        let path = std::path::absolute(path)?;
        Ok(path.strip_prefix(root).unwrap_or(&path).to_path_buf())
    };
    let mut source = source.clone();
    match &mut source {
        TransformSpecSource::Flat { path } => *path = relative(path)?,
        TransformSpecSource::Tree(tree) => {
            tree.config_path = relative(&tree.config_path)?;
            tree.modules_root = relative(&tree.modules_root)?;
            tree.vendor_marks_path = relative(&tree.vendor_marks_path)?;
            tree.out_root = PathBuf::from(APPLY_OUTPUT_ROOT);
            tree.source_root = tree.source_root.as_deref().map(relative).transpose()?;
        }
    }
    Ok(source)
}

/// Filesystem paths only: module IDs and specifiers are semantic strings.
pub(super) fn map_apply_spec_paths(
    spec: &mut TransformSpec,
    map: &mut impl FnMut(ApplyPath, &Path) -> Result<PathBuf>,
) -> Result<()> {
    spec.inputs.input_root = map(ApplyPath::InputRoot, &spec.inputs.input_root)?;
    spec.inputs.js_list_path = map(ApplyPath::JsList, &spec.inputs.js_list_path)?;
    for (field, path) in [
        (
            ApplyPath::EmbeddedLoaders,
            &mut spec.inputs.embedded_loaders_path,
        ),
        (
            ApplyPath::VendorManifest,
            &mut spec.swap_vendor_chunks.output_manifest_path,
        ),
        (
            ApplyPath::VendorWrappers,
            &mut spec.swap_vendor_chunks.output_wrapper_dir,
        ),
        (
            ApplyPath::Reports,
            &mut spec.materialize_logical_modules.report_out_dir,
        ),
    ] {
        if let Some(path) = path {
            *path = map(field, path)?;
        }
    }
    for (chunk, mark) in &mut spec.vendor {
        if let spec::vendor_marks::VendorLevel::BundledPartialSwap(swap) = &mut mark.level {
            swap.bundle.path = map(ApplyPath::VendorBundle(chunk.clone()), &swap.bundle.path)?;
        }
    }
    if let Some(write) = &mut spec.write_js_tree {
        write.out_dir = map(ApplyPath::JsOutput, &write.out_dir)?;
    }
    if let Some(browser) = &mut spec.emit_browser_harness {
        browser.asset_summary_path = map(ApplyPath::BrowserAssets, &browser.asset_summary_path)?;
        browser.out_dir = map(ApplyPath::BrowserOutput, &browser.out_dir)?;
        browser.snapshot_root = map(ApplyPath::BrowserSnapshot, &browser.snapshot_root)?;
    }
    Ok(())
}
