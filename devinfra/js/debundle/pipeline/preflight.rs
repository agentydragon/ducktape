use super::*;

pub(super) fn validate_transform_spec(spec: &TransformSpec) -> Result<()> {
    // A loader behavior is a claim about a loader; without one it is a
    // contradiction, not a no-op (STYLE.md § Configuration consistency).
    if spec.host_behavior.synchronous_module_loader == spec::SynchronousModuleLoader::None {
        let host_behavior = &spec.host_behavior;
        for (key, given) in [
            (
                "loader_result_for_evaluating_module",
                host_behavior.loader_result_for_evaluating_module.is_some(),
            ),
            (
                "synchronous_load_runs_app_callbacks",
                host_behavior.synchronous_load_runs_app_callbacks.is_some(),
            ),
            (
                "synchronous_load_drains_microtasks",
                host_behavior.synchronous_load_drains_microtasks.is_some(),
            ),
        ] {
            anyhow::ensure!(
                !given,
                "host_behavior.{key} requires host_behavior.synchronous_module_loader: present"
            );
        }
    }
    if spec.program_premises.process_prototype_chain_accessors
        != spec::ProcessPrototypeChainAccessors::None
    {
        for builtin in &spec.host_behavior.reentry_free_builtin_modules {
            anyhow::ensure!(
                !builtin.reads_process_accessors(),
                "host_behavior.reentry_free_builtin_modules entry {builtin:?} requires \
                 program_premises.process_prototype_chain_accessors: none"
            );
        }
    }
    anyhow::ensure!(
        !spec
            .host_behavior
            .reentry_free_embedded_loaders
            .contains(&spec::EmbeddedLoader::Napi)
            || spec.program_premises.non_literal_loader_calls
                == spec::NonLiteralLoaderCalls::NativeAddonsOnly,
        "host_behavior.reentry_free_embedded_loaders entry napi requires \
         program_premises.non_literal_loader_calls: native_addons_only"
    );
    if spec.inputs.input_root.as_os_str().is_empty() {
        bail!("Transform spec inputs.input_root must not be empty");
    }
    if spec.inputs.js_list_path.as_os_str().is_empty() {
        bail!("Transform spec inputs.js_list_path must not be empty");
    }
    // Every chunk listed in `logical_modules` (and `chunk_renames`)
    // must have an explicit `unassigned_mode` entry. There is no
    // implicit default — the spec author must state how unclaimed
    // top-level statements get handled per chunk. Chunks that appear
    // only in `unassigned_mode` (e.g. catch-all-only chunks) are
    // valid; the asymmetry is intentional.
    let missing: BTreeSet<&String> = spec
        .logical_modules
        .keys()
        .chain(spec.chunk_renames.keys())
        .filter(|chunk_id| !spec.unassigned_mode.contains_key(*chunk_id))
        .collect();
    if !missing.is_empty() {
        let names = missing
            .iter()
            .map(|s| s.as_str())
            .collect::<Vec<_>>()
            .join(", ");
        bail!(
            "Transform spec is missing `unassigned_mode` entries for chunk(s): {names}. \
             Every chunk listed in `logical_modules` or `chunk_renames` must declare its \
             unassigned_mode explicitly (no implicit default)."
        );
    }
    Ok(())
}

pub(super) fn preflight_output_roots(spec: &TransformSpec) -> Result<()> {
    let mut roots = Vec::<PathBuf>::new();
    if let Some(cfg) = &spec.write_js_tree {
        roots.push(cfg.out_dir.clone());
    }
    if let Some(cfg) = &spec.emit_browser_harness {
        roots.push(cfg.out_dir.clone());
    }
    if let Some(dir) = &spec.materialize_logical_modules.report_out_dir {
        push_if_uncovered(&mut roots, dir.clone());
    }
    if spec.swap_vendor_chunks.write {
        if let Some(path) = &spec.swap_vendor_chunks.output_manifest_path
            && let Some(parent) = path.parent()
        {
            push_if_uncovered(&mut roots, parent.to_path_buf());
        }
        if let Some(dir) = &spec.swap_vendor_chunks.output_wrapper_dir {
            push_if_uncovered(&mut roots, dir.clone());
        }
    }

    preflight_roots(roots)
}

pub(super) fn preflight_roots(mut roots: Vec<PathBuf>) -> Result<()> {
    roots.sort();
    roots.dedup();
    for root in roots {
        if !root.exists() {
            continue;
        }
        if !root.is_dir() {
            bail!(
                "Output path exists and is not a directory: {}",
                root.display()
            );
        }
        if fs::read_dir(&root)?.next().is_some() {
            bail!(
                "Output directory is not empty: {}. Remove it before running debundle.",
                root.display()
            );
        }
    }
    Ok(())
}

pub(super) fn push_if_uncovered(roots: &mut Vec<PathBuf>, path: PathBuf) {
    if roots.iter().any(|root| path_is_within(&path, root)) {
        return;
    }
    roots.push(path);
}

pub(super) fn path_is_within(path: &Path, root: &Path) -> bool {
    path == root || path.starts_with(root)
}
