use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use anyhow::{Context, Result, bail};
use clap::Args as ClapArgs;
use runfiles::{Runfiles, rlocation};
use serde::{Deserialize, Serialize};

use artifact::ArtifactIndexes;
use artifact::load_js_chunks;
use artifact::{ChunkDecompositionOutput, ChunkId};
use emit_harness::{EmitBrowserHarnessOptions, emit_browser_harness};
use lowering::{MaterializeLogicalModulesOptions, materialize_logical_modules};
use prepare_chunks::prepare_js_chunks;
use rewrite_specifiers::rewrite_chunk_entry_specifiers;
use spec::{MaterializeLogicalModulesConfig, SwapVendorChunksConfig, TransformSpec, VendorLevel};
use spec_tree::{CompileSpecTreeOptions, compile_spec_tree};
use strip_swapped_vendor_exports::{ChunkStripStats, strip_swapped_vendor_exports};
use validate_emitted_exports::validate_emitted_exports;
use vendor::{
    ApplyBundledPartialVendorSwapsOptions, ApplyPartialVendorSwapsOptions,
    ChunkBundledPartialSwapResolution, ChunkPartialSwapResolution, SwapVendorOptions,
    VendorResolution, apply_bundled_partial_vendor_swaps, apply_partial_vendor_swaps,
    apply_vendor_annotations, rename_vendor_exports, swap_vendor_chunks,
};
use write_tree::{WriteTreeInput, write_js_tree};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TransformCli {
    pub spec_source: TransformSpecSource,
    pub package_roots: HashMap<String, PathBuf>,
    pub packages_root: Option<PathBuf>,
    pub force: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TransformSpecSource {
    Flat { path: PathBuf },
    Tree(CompileSpecTreeOptions),
}

/// Command-line arguments for the debundle transform pipeline.
///
/// Use [`TransformArgs::resolve`] to obtain a [`TransformCli`] with paths
/// resolved against Bazel runfiles when running as a `bazel run` target.
#[derive(ClapArgs, Debug)]
pub struct TransformArgs {
    /// Path to a flat transform spec YAML.
    #[arg(long)]
    pub spec: Option<PathBuf>,
    /// Path to a tree-shaped authoring config YAML.
    #[arg(long = "tree-config")]
    pub tree_config: Option<PathBuf>,
    /// Root directory containing tree-shaped logical module YAML files.
    #[arg(long = "tree-modules")]
    pub tree_modules: Option<PathBuf>,
    /// Path to tree-shaped vendor marks YAML.
    #[arg(long = "tree-vendor-marks")]
    pub tree_vendor_marks: Option<PathBuf>,
    /// Root for source-relative paths embedded in the tree-shaped config YAML.
    #[arg(long = "tree-source-root")]
    pub tree_source_root: Option<PathBuf>,
    /// Output root used when compiling tree-shaped authoring sources.
    #[arg(long = "out-root")]
    pub out_root: Option<PathBuf>,
    /// Map a package name to its source directory: `<pkg>=<dir>`. May be repeated.
    #[arg(long = "package-root", value_parser = parse_package_root_kv)]
    pub package_roots: Vec<(String, PathBuf)>,
    /// Root directory containing per-package sources (alternative to repeated --package-root).
    #[arg(long)]
    pub packages_root: Option<PathBuf>,
    /// Replace existing output directories for stages that write output trees.
    #[arg(long)]
    pub force: bool,
}

impl TransformArgs {
    /// Resolve all path arguments against Bazel runfiles (when present) and
    /// collapse `--package-root` pairs into a `HashMap`.
    pub fn resolve(self) -> Result<TransformCli> {
        let runfiles = Runfiles::create().ok();
        let spec_source = resolve_spec_source(&self, runfiles.as_ref())?;
        Ok(TransformCli {
            spec_source,
            package_roots: self
                .package_roots
                .into_iter()
                .map(|(name, dir)| (name, resolve_runfiles_path(dir, runfiles.as_ref())))
                .collect(),
            packages_root: self
                .packages_root
                .map(|dir| resolve_runfiles_path(dir, runfiles.as_ref())),
            force: self.force,
        })
    }
}

fn resolve_spec_source(
    args: &TransformArgs,
    runfiles: Option<&Runfiles>,
) -> Result<TransformSpecSource> {
    match (&args.spec, &args.tree_config) {
        (Some(_), Some(_)) => {
            bail!("pass either --spec or --tree-config, not both");
        }
        (Some(path), None) => Ok(TransformSpecSource::Flat {
            path: resolve_runfiles_path(path.clone(), runfiles),
        }),
        (None, Some(config_path)) => {
            let modules_root = required_tree_arg("--tree-modules", &args.tree_modules)?;
            let vendor_marks_path =
                required_tree_arg("--tree-vendor-marks", &args.tree_vendor_marks)?;
            let out_root = required_tree_arg("--out-root", &args.out_root)?;
            Ok(TransformSpecSource::Tree(CompileSpecTreeOptions {
                config_path: resolve_runfiles_path(config_path.clone(), runfiles),
                modules_root: resolve_runfiles_path(modules_root, runfiles),
                vendor_marks_path: resolve_runfiles_path(vendor_marks_path, runfiles),
                source_root: args
                    .tree_source_root
                    .clone()
                    .map(|path| resolve_runfiles_path(path, runfiles)),
                out_root: resolve_runfiles_path(out_root, runfiles),
                force: args.force,
            }))
        }
        (None, None) => {
            bail!("pass either --spec or --tree-config");
        }
    }
}

fn required_tree_arg(name: &str, value: &Option<PathBuf>) -> Result<PathBuf> {
    value
        .clone()
        .with_context(|| format!("{name} is required with --tree-config"))
}

fn parse_package_root_kv(value: &str) -> Result<(String, PathBuf), String> {
    let Some(separator) = value.find('=') else {
        return Err(format!(
            "--package-root must be in <package>=<dir> form, got {value}"
        ));
    };
    if separator == 0 || separator == value.len() - 1 {
        return Err(format!(
            "--package-root must be in <package>=<dir> form, got {value}"
        ));
    }
    Ok((
        value[..separator].to_string(),
        PathBuf::from(&value[separator + 1..]),
    ))
}

#[derive(Debug, Clone, Serialize)]
pub struct TransformRunSummary {
    pub duration: Duration,
    pub spec_path: String,
    pub preparation_steps: Vec<TransformStepSummary>,
    pub steps: Vec<TransformStepSummary>,
}

#[derive(Debug, Clone, Serialize)]
pub struct TransformStepSummary {
    pub stage: PipelineStage,
    pub duration: Duration,
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PipelineStage {
    LoadTransformSpec,
    ValidateTransformSpec,
    LoadJsChunks,
    PrepareJsChunks,
    BuildArtifactIndexes,
    RewriteChunkEntrySpecifiers,
    ApplyVendorAnnotations,
    RenameVendorExports,
    SwapVendorChunks,
    MaterializeLogicalModules,
    ApplyPartialVendorSwaps,
    ApplyBundledPartialVendorSwaps,
    StripSwappedVendorExports,
    ValidateEmittedExports,
    WriteJsTree,
    EmitBrowserHarness,
}

impl PipelineStage {
    fn report_name(self) -> String {
        match serde_json::to_value(self).expect("PipelineStage serializes as a string") {
            serde_json::Value::String(name) => name,
            _ => unreachable!("PipelineStage unit enum should serialize as a string"),
        }
    }
}

/// Resolve a path through Bazel runfiles when present, otherwise pass through.
///
/// Lets the binary work as a standalone CLI (filesystem paths) and as a
/// Bazel-run target (runfiles-relative paths produced by `$(rlocationpath ...)`)
/// without a launcher wrapper. A path is treated as runfiles-relative only
/// when it actually resolves to a file inside the runfiles tree; otherwise
/// it's left for the caller's filesystem semantics.
fn resolve_runfiles_path(path: PathBuf, runfiles: Option<&Runfiles>) -> PathBuf {
    if path.is_absolute() {
        return path;
    }
    let Some(runfiles) = runfiles else {
        return path;
    };
    let Some(s) = path.to_str() else {
        return path;
    };
    rlocation!(runfiles, s)
        .filter(|resolved| resolved.exists())
        .unwrap_or(path)
}

pub fn render_transform_summary(summary: &TransformRunSummary) -> String {
    let mut out = format!(
        "Ran {} transform steps from {} in {}\n",
        summary.steps.len(),
        summary.spec_path,
        humantime::format_duration(summary.duration)
    );
    for step in &summary.preparation_steps {
        out.push_str(&format!(
            "- {} ({})\n",
            step.stage.report_name(),
            humantime::format_duration(step.duration),
        ));
    }
    for step in &summary.steps {
        out.push_str(&format!(
            "- {} ({})\n",
            step.stage.report_name(),
            humantime::format_duration(step.duration),
        ));
    }
    out
}

pub fn run_transform_cli(cli: &TransformCli) -> Result<TransformRunSummary> {
    let started = Instant::now();
    let mut preparation_steps = Vec::new();
    let spec = run_step_with_result(
        &mut preparation_steps,
        PipelineStage::LoadTransformSpec,
        || load_transform_spec_source(&cli.spec_source),
    )?;
    run_step(
        &mut preparation_steps,
        PipelineStage::ValidateTransformSpec,
        || {
            validate_transform_spec(&spec)?;
            preflight_output_roots(&spec)
        },
    )?;
    let (artifact, _load_manifest) =
        run_step_with_result(&mut preparation_steps, PipelineStage::LoadJsChunks, || {
            load_js_chunks(&spec.inputs.input_root, &spec.inputs.js_list_path)
        })?;
    let materialise_chunk_ids: Vec<String> = spec
        .logical_modules
        .keys()
        .chain(spec.unassigned_mode.keys())
        .chain(spec.chunk_renames.keys())
        .cloned()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let prepare_result = run_step_with_result(
        &mut preparation_steps,
        PipelineStage::PrepareJsChunks,
        || prepare_js_chunks(&spec, artifact),
    )?;
    let artifact_indexes = run_step_with_result(
        &mut preparation_steps,
        PipelineStage::BuildArtifactIndexes,
        || ArtifactIndexes::build(&prepare_result.artifact),
    )?;
    let mut steps = Vec::new();

    let rewrite_result = run_step_with_result(
        &mut steps,
        PipelineStage::RewriteChunkEntrySpecifiers,
        || rewrite_chunk_entry_specifiers(prepare_result.artifact, &artifact_indexes),
    )?;
    let mut artifact = rewrite_result.artifact;
    let counts = prepare_result.counts;
    let mut chunk_records = prepare_result.chunk_records;
    let mut vendor_swaps_report = VendorSwapsReport::default();

    // Vendor stages: each is internally filtered by `level`, so it's
    // safe to always invoke them when `vendor` carries any entries.
    // `apply` runs unconditionally; `rename` and `swap` short-circuit
    // to no-ops when no entry has the right level.
    if !spec.vendor.is_empty() {
        run_step(&mut steps, PipelineStage::ApplyVendorAnnotations, || {
            apply_vendor_annotations(&artifact, &spec.vendor).map(|_| ())
        })?;
        if spec
            .vendor
            .values()
            .any(|m| matches!(m.level, VendorLevel::BoundaryRename | VendorLevel::Swap(_)))
        {
            let rename_result =
                run_step_with_result(&mut steps, PipelineStage::RenameVendorExports, || {
                    rename_vendor_exports(artifact, &spec.vendor, &artifact_indexes)
                })?;
            artifact = rename_result.artifact;
        }
        if spec
            .vendor
            .values()
            .any(|m| matches!(m.level, VendorLevel::Swap(_)))
        {
            let SwapVendorChunksConfig {
                output_manifest_path,
                output_wrapper_dir,
                write,
            } = spec.swap_vendor_chunks.clone();
            let swap_result =
                run_step_with_result(&mut steps, PipelineStage::SwapVendorChunks, || {
                    swap_vendor_chunks(
                        artifact,
                        &spec.vendor,
                        &artifact_indexes,
                        SwapVendorOptions {
                            package_roots: &cli.package_roots,
                            packages_root: &cli.packages_root,
                            output_manifest_path,
                            output_wrapper_dir,
                            write,
                        },
                    )
                })?;
            artifact = swap_result.artifact;
            vendor_swaps_report.full = swap_result.manifest.resolutions;
            chunk_records.retain(|chunk| !swap_result.removed_chunk_ids.contains(&chunk.chunk_id));
        }
    }

    let mut module_count: usize = 0;
    let mut selected_lowerings: Vec<artifact::SelectedModuleLowering> = Vec::new();
    let mut decomposition_by_chunk: HashMap<ChunkId, ChunkDecompositionOutput> = HashMap::new();
    if !materialise_chunk_ids.is_empty() {
        let MaterializeLogicalModulesConfig {
            file,
            prune_other_chunks,
            force,
            report_out_dir,
            target_dir,
        } = spec.materialize_logical_modules.clone();
        let force = force || cli.force;
        let materialize_result =
            run_step_with_result(&mut steps, PipelineStage::MaterializeLogicalModules, || {
                materialize_logical_modules(
                    artifact,
                    &spec.logical_modules,
                    &spec.chunk_renames,
                    &spec.unassigned_mode,
                    &spec.chunk_analysis_options,
                    MaterializeLogicalModulesOptions {
                        chunk_ids: materialise_chunk_ids,
                        file,
                        prune_other_chunks,
                        force,
                        report_out_dir,
                        target_dir,
                    },
                )
            })?;
        artifact = materialize_result.artifact;
        module_count = materialize_result.module_count;
        selected_lowerings = materialize_result.selected_lowerings;
        decomposition_by_chunk = materialize_result.decomposition_by_chunk;
    }

    // Partial-vendor-swap runs *after* materialize so the per-symbol
    // identifier rewrite (`zodObject(...)` → `z.object(...)`) operates
    // on the already-materialized module files. If it ran before
    // materialize, the rewrite would erase the binding names that
    // `binding_patches` / `logical_modules` selectors still rely on at
    // materialize-time (e.g. `anonymous_statements: match: "ee({...})"`).
    let has_partial_swaps = spec
        .vendor
        .values()
        .any(|m| matches!(m.level, VendorLevel::PartialSwap(_)));
    let has_bundled_partial_swaps = spec
        .vendor
        .values()
        .any(|m| matches!(m.level, VendorLevel::BundledPartialSwap(_)));
    if !spec.vendor.is_empty() && (has_partial_swaps || has_bundled_partial_swaps) {
        if has_partial_swaps {
            let partial_result =
                run_step_with_result(&mut steps, PipelineStage::ApplyPartialVendorSwaps, || {
                    apply_partial_vendor_swaps(
                        artifact,
                        &spec.vendor,
                        &artifact_indexes,
                        ApplyPartialVendorSwapsOptions {
                            package_roots: &cli.package_roots,
                            packages_root: &cli.packages_root,
                        },
                    )
                })?;
            artifact = partial_result.artifact;
            vendor_swaps_report.partial = partial_result.manifest.resolutions;
        }

        if has_bundled_partial_swaps {
            let SwapVendorChunksConfig {
                output_manifest_path,
                output_wrapper_dir,
                write,
            } = spec.swap_vendor_chunks.clone();
            let bundled_result = run_step_with_result(
                &mut steps,
                PipelineStage::ApplyBundledPartialVendorSwaps,
                || {
                    apply_bundled_partial_vendor_swaps(
                        artifact,
                        &spec.vendor,
                        &artifact_indexes,
                        ApplyBundledPartialVendorSwapsOptions {
                            package_roots: &cli.package_roots,
                            packages_root: &cli.packages_root,
                            output_manifest_path,
                            output_wrapper_dir,
                            write,
                        },
                    )
                },
            )?;
            artifact = bundled_result.artifact;
            vendor_swaps_report.bundled_partial = bundled_result.manifest.resolutions;
        }

        // The consumer side has been rewritten to import each swapped
        // symbol from upstream; drop the vendor chunk's residual
        // `export { … }` entries and any top-level bindings that are
        // unreachable once those exports are gone.
        let strip_result =
            run_step_with_result(&mut steps, PipelineStage::StripSwappedVendorExports, || {
                strip_swapped_vendor_exports(artifact, &spec.vendor)
            })?;
        artifact = strip_result.artifact;
        vendor_swaps_report.strip_stats = strip_result.manifest.per_chunk;
    }

    write_vendor_swaps_report(
        spec.swap_vendor_chunks.write,
        spec.swap_vendor_chunks.output_manifest_path.as_deref(),
        &vendor_swaps_report,
    )?;

    // Final emit-shape check: every JS file that came out of the
    // materialize / strip pipeline must have unique public export
    // names per file. Catching a duplicate here turns a downstream
    // browser-link failure (which Chromium reports as a silent empty
    // pageerror) into an immediate build-time error pointing at the
    // exact file, name, and source lines. Runs unconditionally so
    // pipelines without vendor swaps still benefit.
    run_step(&mut steps, PipelineStage::ValidateEmittedExports, || {
        validate_emitted_exports(&artifact)
    })?;

    if let Some(cfg) = &spec.write_js_tree {
        let out_dir = cfg.out_dir.clone();
        let force = cfg.force || cli.force;
        run_step(&mut steps, PipelineStage::WriteJsTree, || {
            write_js_tree(&WriteTreeInput {
                artifact: &artifact,
                out_dir: &out_dir,
                force,
                lowerings: &selected_lowerings,
                counts: &counts,
                chunk_records: &chunk_records,
                module_count,
                decomposition_by_chunk: &decomposition_by_chunk,
            })
        })?;
    }

    if let Some(cfg) = &spec.emit_browser_harness {
        let force = cfg.force || cli.force;
        let opts = EmitBrowserHarnessOptions {
            asset_summary_path: cfg.asset_summary_path.clone(),
            force,
            out_dir: cfg.out_dir.clone(),
            snapshot_root: cfg.snapshot_root.clone(),
        };
        run_step(&mut steps, PipelineStage::EmitBrowserHarness, || {
            emit_browser_harness(&artifact, &opts, &chunk_records, &decomposition_by_chunk)?;
            Ok(())
        })?;
    }

    Ok(TransformRunSummary {
        duration: started.elapsed(),
        spec_path: spec_source_description(&cli.spec_source),
        preparation_steps,
        steps,
    })
}

fn load_transform_spec_source(source: &TransformSpecSource) -> Result<TransformSpec> {
    match source {
        TransformSpecSource::Flat { path } => load_flat_transform_spec(path),
        TransformSpecSource::Tree(options) => compile_spec_tree(options),
    }
}

fn spec_source_description(source: &TransformSpecSource) -> String {
    match source {
        TransformSpecSource::Flat { path } => path.display().to_string(),
        TransformSpecSource::Tree(options) => options.config_path.display().to_string(),
    }
}

fn run_step(
    steps: &mut Vec<TransformStepSummary>,
    stage: PipelineStage,
    body: impl FnOnce() -> Result<()>,
) -> Result<()> {
    let started = Instant::now();
    body()?;
    steps.push(TransformStepSummary {
        stage,
        duration: started.elapsed(),
    });
    Ok(())
}

fn run_step_with_result<T>(
    steps: &mut Vec<TransformStepSummary>,
    stage: PipelineStage,
    body: impl FnOnce() -> Result<T>,
) -> Result<T> {
    let started = Instant::now();
    let value = body()?;
    steps.push(TransformStepSummary {
        stage,
        duration: started.elapsed(),
    });
    Ok(value)
}

fn load_flat_transform_spec(spec_path: &Path) -> Result<TransformSpec> {
    let raw = fs::read(spec_path).with_context(|| format!("reading {}", spec_path.display()))?;
    serde_yaml::from_slice(&raw)
        .with_context(|| format!("Failed to parse {} as YAML", spec_path.display()))
}

fn validate_transform_spec(spec: &TransformSpec) -> Result<()> {
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

#[derive(Debug, Default, Serialize)]
struct VendorSwapsReport {
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    full: BTreeMap<String, VendorResolution>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    partial: BTreeMap<String, ChunkPartialSwapResolution>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    bundled_partial: BTreeMap<String, ChunkBundledPartialSwapResolution>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    strip_stats: BTreeMap<String, ChunkStripStats>,
}

fn write_vendor_swaps_report(
    write: bool,
    path: Option<&Path>,
    report: &VendorSwapsReport,
) -> Result<()> {
    if !write {
        return Ok(());
    }
    if report.full.is_empty()
        && report.partial.is_empty()
        && report.bundled_partial.is_empty()
        && report.strip_stats.is_empty()
    {
        return Ok(());
    }
    let Some(path) = path else {
        return Ok(());
    };
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::write(path, serde_json::to_string_pretty(report)?)?;
    Ok(())
}

fn preflight_output_roots(spec: &TransformSpec) -> Result<()> {
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

fn push_if_uncovered(roots: &mut Vec<PathBuf>, path: PathBuf) {
    if roots.iter().any(|root| path_is_within(&path, root)) {
        return;
    }
    roots.push(path);
}

fn path_is_within(path: &Path, root: &Path) -> bool {
    path == root || path.starts_with(root)
}

#[cfg(test)]
mod tests {
    use super::*;
    use artifact::parse_js_list;
    use std::collections::BTreeMap;

    #[derive(Serialize)]
    struct AssetSummaryFixture<'a> {
        #[serde(rename = "entryPoints")]
        entry_points: AssetSummaryEntryPoints<'a>,
    }

    #[derive(Serialize)]
    struct AssetSummaryEntryPoints<'a> {
        html: &'a str,
    }

    #[derive(Serialize)]
    struct PipelineSpecFixture<'a> {
        inputs: PipelineSpecInputs<'a>,
        emit_browser_harness: EmitBrowserHarnessFixture<'a>,
    }

    #[derive(Serialize)]
    struct PipelineSpecInputs<'a> {
        input_root: &'a Path,
        js_list_path: &'a Path,
    }

    #[derive(Serialize)]
    struct EmitBrowserHarnessFixture<'a> {
        asset_summary_path: &'a Path,
        force: bool,
        out_dir: &'a Path,
        snapshot_root: &'a Path,
    }

    #[test]
    fn parse_js_list_rejects_duplicates() {
        js_ast::with_swc_globals(|| {
            let err = parse_js_list("a.js\na.js\n").expect_err("expected duplicate rejection");
            assert!(err.to_string().contains("duplicate"));
        });
    }

    #[test]
    fn parse_js_list_ignores_comments_and_blank_lines() {
        js_ast::with_swc_globals(|| {
            let parsed = parse_js_list("\n# comment\nfoo.js\nbar.js\n").expect("parse list");
            assert_eq!(parsed, vec!["foo.js", "bar.js"]);
        });
    }

    #[test]
    fn run_transform_cli_writes_spec_pipeline_outputs() -> Result<()> {
        js_ast::with_swc_globals(|| {
            let temp = tempfile::tempdir()?;
            let root = temp.path();
            let snapshot = root.join("snapshot");
            let extracted = root.join("extracted");
            let out = root.join("out");
            fs::create_dir_all(snapshot.join("static"))?;
            fs::create_dir_all(&extracted)?;
            fs::write(
                snapshot.join("index.html"),
                r#"<!doctype html>
<html>
  <head>
    <link rel="modulepreload" href="./static/chunk-DuckMock.js">
  </head>
  <body>
    <script type="module" src="./static/index-DuckMock.js"></script>
  </body>
</html>
"#,
            )?;
            fs::write(
                snapshot.join("static/index-DuckMock.js"),
                "import { y } from './chunk-DuckMock.js';\nglobalThis.__value = y;\n",
            )?;
            fs::write(
                snapshot.join("static/chunk-DuckMock.js"),
                "export const y = 2;\n",
            )?;
            fs::write(
                extracted.join("js-files.txt"),
                "static/index-DuckMock.js\nstatic/chunk-DuckMock.js\n",
            )?;
            let js_list_path = extracted.join("js-files.txt");
            let asset_summary_path = extracted.join("asset-summary.json");
            fs::write(
                &asset_summary_path,
                serde_json::to_string(&AssetSummaryFixture {
                    entry_points: AssetSummaryEntryPoints { html: "index.html" },
                })?,
            )?;
            let spec_path = root.join("transform-spec.yaml");
            fs::write(
                &spec_path,
                serde_yaml::to_string(&PipelineSpecFixture {
                    inputs: PipelineSpecInputs {
                        input_root: &snapshot,
                        js_list_path: &js_list_path,
                    },
                    emit_browser_harness: EmitBrowserHarnessFixture {
                        asset_summary_path: &asset_summary_path,
                        force: true,
                        out_dir: &out,
                        snapshot_root: &snapshot,
                    },
                })?,
            )?;

            let summary = run_transform_cli(&TransformCli {
                spec_source: TransformSpecSource::Flat { path: spec_path },
                package_roots: HashMap::new(),
                packages_root: None,
                force: false,
            })?;

            assert_eq!(summary.steps.len(), 3);
            assert_eq!(summary.preparation_steps.len(), 5);
            let rendered_summary = render_transform_summary(&summary);
            assert!(rendered_summary.contains("- load_transform_spec"));
            assert!(rendered_summary.contains("- prepare_js_chunks"));
            assert!(rendered_summary.contains("- build_artifact_indexes"));
            assert!(rendered_summary.contains("- rewrite_chunk_entry_specifiers"));
            assert!(rendered_summary.contains("- validate_emitted_exports"));
            assert!(rendered_summary.contains("- emit_browser_harness"));
            assert!(out.join("app/bootstrap.js").exists());
            assert!(out.join("reports/runtime.json").exists());
            let entry = fs::read_to_string(out.join("app/static/index-DuckMock/entry.js"))?;
            assert!(entry.contains("../chunk-DuckMock/entry.js"));
            let chunk_entry = fs::read_to_string(out.join("app/static/chunk-DuckMock/entry.js"))?;
            let total_bytes = entry.len() + chunk_entry.len();
            let total_lines = entry.lines().count() + chunk_entry.lines().count();

            let output: serde_json::Value =
                serde_json::from_str(&fs::read_to_string(out.join("reports/output.json"))?)?;
            assert_eq!(
                output
                    .pointer("/output_metrics/total/files")
                    .and_then(serde_json::Value::as_u64),
                Some(2),
            );
            assert_eq!(
                output
                    .pointer("/output_metrics/total/bytes")
                    .and_then(serde_json::Value::as_u64),
                Some(total_bytes as u64),
            );
            assert_eq!(
                output
                    .pointer("/output_metrics/total/lines")
                    .and_then(serde_json::Value::as_u64),
                Some(total_lines as u64),
            );
            assert_eq!(
                output
                    .pointer("/output_metrics/top_level_entry/files")
                    .and_then(serde_json::Value::as_u64),
                Some(2),
            );
            assert_eq!(
                output
                    .pointer("/output_metrics/named_modules/files")
                    .and_then(serde_json::Value::as_u64),
                Some(0),
            );
            assert_eq!(
                output
                    .pointer("/output_metrics/largest_files_by_bytes/0/role")
                    .and_then(serde_json::Value::as_str),
                Some("top_level_entry"),
            );
            let runtime: serde_json::Value =
                serde_json::from_str(&fs::read_to_string(out.join("reports/runtime.json"))?)?;
            assert!(
                runtime.get("schema_version").is_none(),
                "runtime report should not carry a compatibility schema_version"
            );
            assert!(
                runtime.get("parse_plan").is_none(),
                "runtime report should not carry a parse_plan field"
            );
            assert_eq!(
                runtime.get("app_root").and_then(serde_json::Value::as_str),
                Some("../app")
            );
            assert_eq!(
                runtime
                    .pointer("/generated/bootstrap")
                    .and_then(serde_json::Value::as_str),
                Some("bootstrap.js")
            );
            assert!(
                out.join("reports/source_assets.json").exists(),
                "source asset metadata should live under reports/"
            );
            assert!(
                out.join("reports/provenance.json").exists(),
                "source provenance should live under reports/"
            );
            let chunks_manifest: serde_json::Value =
                serde_json::from_str(&fs::read_to_string(out.join("reports/chunks.json"))?)?;
            assert!(
                chunks_manifest.get("schema_version").is_none(),
                "chunks report should not carry a compatibility schema_version"
            );
            assert_eq!(
                chunks_manifest
                    .get("chunks")
                    .and_then(serde_json::Value::as_array)
                    .map(Vec::len),
                Some(2)
            );
            Ok(())
        })
    }

    #[test]
    fn cli_force_does_not_replace_non_empty_outputs() -> Result<()> {
        js_ast::with_swc_globals(|| {
            let temp = tempfile::tempdir()?;
            let root = temp.path();
            let snapshot = root.join("snapshot");
            let extracted = root.join("extracted");
            let js_out = root.join("js-out");
            let harness_out = root.join("harness-out");
            fs::create_dir_all(snapshot.join("static"))?;
            fs::create_dir_all(&extracted)?;
            fs::create_dir_all(&js_out)?;
            fs::create_dir_all(&harness_out)?;
            fs::write(js_out.join("stale.txt"), "old")?;
            fs::write(harness_out.join("stale.txt"), "old")?;
            fs::write(
                snapshot.join("index.html"),
                r#"<!doctype html>
<script type="module" src="./static/index.js"></script>
"#,
            )?;
            fs::write(
                snapshot.join("static/index.js"),
                "globalThis.__value = 1;\n",
            )?;
            let js_list_path = extracted.join("js-files.txt");
            fs::write(&js_list_path, "static/index.js\n")?;
            let asset_summary_path = extracted.join("asset-summary.json");
            fs::write(
                &asset_summary_path,
                serde_json::to_string(&AssetSummaryFixture {
                    entry_points: AssetSummaryEntryPoints { html: "index.html" },
                })?,
            )?;

            let mut spec = empty_transform_spec();
            spec.inputs = spec::LoadJsChunksArgs {
                input_root: snapshot.clone(),
                js_list_path,
            };
            spec.write_js_tree = Some(spec::WriteJsTreeConfig {
                out_dir: js_out.clone(),
                force: false,
            });
            spec.emit_browser_harness = Some(spec::EmitBrowserHarnessConfig {
                asset_summary_path,
                out_dir: harness_out.clone(),
                snapshot_root: snapshot,
                force: false,
            });
            let spec_path = root.join("transform-spec.yaml");
            fs::write(&spec_path, serde_yaml::to_string(&spec)?)?;

            let err = run_transform_cli(&TransformCli {
                spec_source: TransformSpecSource::Flat { path: spec_path },
                package_roots: HashMap::new(),
                packages_root: None,
                force: true,
            })
            .expect_err("non-empty output directories should be rejected, not replaced");
            assert!(
                err.to_string().contains("Output directory is not empty"),
                "unexpected error: {err:#}"
            );
            assert!(js_out.join("stale.txt").exists());
            assert!(harness_out.join("stale.txt").exists());
            Ok(())
        })
    }

    fn empty_transform_spec() -> TransformSpec {
        TransformSpec {
            inputs: spec::LoadJsChunksArgs {
                input_root: PathBuf::from("."),
                js_list_path: PathBuf::from("js-files.txt"),
            },
            vendor: BTreeMap::new(),
            logical_modules: BTreeMap::new(),
            chunk_renames: BTreeMap::new(),
            unassigned_mode: BTreeMap::new(),
            chunk_analysis_options: BTreeMap::new(),
            swap_vendor_chunks: SwapVendorChunksConfig::default(),
            materialize_logical_modules: MaterializeLogicalModulesConfig::default(),
            write_js_tree: None,
            emit_browser_harness: None,
        }
    }
}
