use super::*;

pub(super) fn provenance_roots(
    cli: &TransformRequest,
) -> Result<(ProvenanceRoots, Option<PathBuf>)> {
    let (spec_root, modules_directory) = match &cli.spec_source {
        TransformSpecSource::Flat { path } => (
            graph_provenance::absolute(
                path.parent()
                    .filter(|parent| !parent.as_os_str().is_empty())
                    .unwrap_or_else(|| Path::new(".")),
            )?,
            None,
        ),
        TransformSpecSource::Tree(tree) => {
            let config_parent = graph_provenance::absolute(
                tree.config_path
                    .parent()
                    .filter(|parent| !parent.as_os_str().is_empty())
                    .unwrap_or_else(|| Path::new(".")),
            )?;
            let vendor_parent = graph_provenance::absolute(
                tree.vendor_marks_path
                    .parent()
                    .filter(|parent| !parent.as_os_str().is_empty())
                    .unwrap_or_else(|| Path::new(".")),
            )?;
            let modules_root = graph_provenance::absolute(&tree.modules_root)?;
            let mut common = config_parent;
            while !vendor_parent.starts_with(&common) || !modules_root.starts_with(&common) {
                common = common
                    .parent()
                    .context("spec inputs have no common root")?
                    .to_path_buf();
            }
            let modules_directory = modules_root.strip_prefix(&common)?.to_path_buf();
            (common, Some(modules_directory))
        }
    };
    let source = match &cli.spec_source {
        TransformSpecSource::Tree(tree) => tree
            .source_root
            .as_deref()
            .map(graph_provenance::absolute)
            .transpose()?,
        TransformSpecSource::Flat { .. } => None,
    }
    .unwrap_or(std::env::current_dir()?);
    let packages =
        graph_provenance::capture_package_roots(&cli.package_roots, cli.packages_root.as_deref())?;
    Ok((
        ProvenanceRoots {
            spec: spec_root,
            source,
            ownership_subset: None,
            packages,
        },
        modules_directory,
    ))
}

/// A runnable argv containing the effective inputs, including values supplied
/// only through Clap env vars. All input and output roots are absolute so a
/// caller can replay it from another working directory (or translate roots
/// for an isolated snapshot).
pub(super) fn canonical_generation_command(
    cli: &TransformRequest,
    options: &TransformRunOptions,
    roots: &ProvenanceRoots,
    loaded_analysis: Option<&Path>,
) -> Result<Vec<String>> {
    fn resolved_arg(path: &Path) -> Result<String> {
        Ok(path
            .to_str()
            .with_context(|| format!("non-UTF-8 generation path {}", path.display()))?
            .to_string())
    }
    fn path_arg(path: &Path) -> Result<String> {
        resolved_arg(&graph_provenance::absolute(path)?)
    }
    let mut argv = vec![path_arg(&std::env::current_exe()?)?, "run".to_string()];
    match &cli.spec_source {
        TransformSpecSource::Flat { path } => {
            argv.extend(["--spec".to_string(), path_arg(path)?]);
        }
        TransformSpecSource::Tree(tree) => {
            argv.extend(["--tree-config".to_string(), path_arg(&tree.config_path)?]);
            argv.extend(["--tree-modules".to_string(), path_arg(&tree.modules_root)?]);
            argv.extend([
                "--tree-vendor-marks".to_string(),
                path_arg(&tree.vendor_marks_path)?,
            ]);
            // Explicitly preserve the effective compile root when it came
            // from DEBUNDLE_TREE_SOURCE_ROOT or the original cwd.
            argv.extend([
                "--tree-source-root".to_string(),
                resolved_arg(&roots.source)?,
            ]);
            argv.extend(["--out-root".to_string(), path_arg(&tree.out_root)?]);
        }
    }
    for (name, path) in &roots.packages {
        if name != graph_provenance::SHARED_PACKAGES_ROOT {
            argv.extend([
                "--package-root".to_string(),
                format!("{name}={}", resolved_arg(path)?),
            ]);
        }
    }
    if let Some(input) = &options.ownership_subset_input {
        argv.extend(["--ownership-subset".to_string(), path_arg(&input.path)?]);
    }
    if let Some(root) = roots.packages.get(graph_provenance::SHARED_PACKAGES_ROOT) {
        argv.extend(["--packages-root".to_string(), resolved_arg(root)?]);
    }
    match options.output {
        RunOutput::DryRun => argv.push("--dry-run".to_string()),
        RunOutput::EmitOnRejection => argv.push("--emit-on-rejection".to_string()),
        RunOutput::Accepted => {}
    }
    if options.graph_reports.enabled() {
        argv.push("--graphs".to_string());
    }
    if let RunArtifacts::ReportsOnly { out_root } = &options.artifacts {
        argv.extend(["--reports-only".to_string(), path_arg(out_root)?]);
        argv.extend([
            "--analysis".to_string(),
            path_arg(loaded_analysis.context("reports-only requires loaded analysis")?)?,
        ]);
    }
    if !options.keep_going {
        argv.push("--fail-fast".to_string());
    }
    if options.effect_conflicts_jsonl {
        argv.push("--effect-conflicts-jsonl".to_string());
    }
    for chunk in &options.effect_conflicts_chunks {
        argv.extend(["--effect-conflicts-chunk".to_string(), chunk.clone()]);
    }
    for report in &options.full_reports {
        argv.extend(["--full-report".to_string(), report.to_string()]);
    }
    for chunk in &options.full_report_chunks {
        argv.extend(["--full-report-chunk".to_string(), chunk.clone()]);
    }
    Ok(argv)
}
