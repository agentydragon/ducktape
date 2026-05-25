use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use anyhow::{Context, Result, bail};
use clap::Args as ClapArgs;
use serde::Serialize;

use artifact::ArtifactIndexes;
use artifact::ChunkBundle;
use artifact::load_js_chunks;
use artifact::write_json;
use artifact::{ChunkDecompositionOutput, ChunkId};
use emit_harness::{EmitBrowserHarnessOptions, emit_browser_harness};
use lowering::{MaterializeLogicalModulesOptions, materialize_logical_modules};
use output_layout::REPORTS_DIR;
use prepare_chunks::prepare_js_chunks;
use rewrite_specifiers::rewrite_chunk_entry_specifiers;
use spec::{MaterializeLogicalModulesConfig, TransformSpec, VendorLevel};
use spec_tree::{CompileSpecTreeOptions, compile_spec_tree};
use validate_emitted_exports::validate_emitted_exports;
use vendor::{
    ApplyBundledPartialVendorSwapsOptions, ApplyPartialVendorSwapsOptions,
    ChunkBundledPartialSwapResolution, ChunkPartialSwapResolution, ChunkStripStats,
    StripSwappedVendorExportsOptions, SwapVendorOptions, VendorResolution,
    apply_bundled_partial_vendor_swaps, apply_partial_vendor_swaps, apply_vendor_annotations,
    rename_vendor_exports, strip_swapped_vendor_exports_with_options, swap_vendor_chunks,
};
use write_tree::{WriteTreeInput, write_js_tree};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TransformCli {
    pub spec_source: TransformSpecSource,
    pub package_roots: HashMap<String, PathBuf>,
    pub packages_root: Option<PathBuf>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TransformSpecSource {
    Flat { path: PathBuf },
    Tree(CompileSpecTreeOptions),
}

/// Command-line arguments for the debundle transform pipeline.
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
}

impl TransformArgs {
    /// Collapse `--package-root` pairs into a `HashMap` and validate spec source.
    pub fn resolve(self) -> Result<TransformCli> {
        let spec_source = resolve_spec_source(&self)?;
        Ok(TransformCli {
            spec_source,
            package_roots: self.package_roots.into_iter().collect(),
            packages_root: self.packages_root,
        })
    }
}

fn resolve_spec_source(args: &TransformArgs) -> Result<TransformSpecSource> {
    match (&args.spec, &args.tree_config) {
        (Some(_), Some(_)) => {
            bail!("pass either --spec or --tree-config, not both");
        }
        (Some(path), None) => Ok(TransformSpecSource::Flat { path: path.clone() }),
        (None, Some(config_path)) => {
            let modules_root = required_tree_arg("--tree-modules", &args.tree_modules)?;
            let vendor_marks_path =
                required_tree_arg("--tree-vendor-marks", &args.tree_vendor_marks)?;
            let out_root = required_tree_arg("--out-root", &args.out_root)?;
            Ok(TransformSpecSource::Tree(CompileSpecTreeOptions {
                config_path: config_path.clone(),
                modules_root,
                vendor_marks_path,
                source_root: args.tree_source_root.clone(),
                out_root,
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

/// Wall-clock timings for every meaningful top-level phase of
/// `run_transform_cli`. Sub-phase timings (chunk-level analysis,
/// per-plan emit, etc.) continue to live in `modules.json`; this
/// struct is the pipeline-level peer that captures the work between
/// `load_js_chunks` and `emit_browser_harness` so the previously
/// uninstrumented gap in profiles disappears. Phases marked
/// `prepare_js_chunks.parse_files_sum` / `analyze_files_sum`
/// aggregate the per-file `ParsedJsFileRecord` durations the parser
/// already records, so the cost of the parse step relative to the
/// surrounding orchestration is visible without re-instrumenting
/// `prepare_js_chunks`.
#[derive(Debug, Default, Serialize)]
struct PipelineTimings {
    durations: BTreeMap<String, Duration>,
}

impl PipelineTimings {
    fn add(&mut self, name: &str, duration: Duration) {
        *self.durations.entry(name.to_string()).or_default() += duration;
    }

    fn into_report(self, total: Duration) -> PipelineTimingsReport {
        let mut durations = self.durations;
        durations.insert("total".to_string(), total);
        let durations_ms: BTreeMap<String, u128> = durations
            .iter()
            .map(|(k, v)| (k.clone(), v.as_millis()))
            .collect();
        PipelineTimingsReport {
            durations,
            durations_ms,
        }
    }
}

/// `pipeline.json` shape. `durations` serialises each phase's
/// `Duration` (`{secs, nanos}`); `durations_ms` is the same data
/// flattened to integer milliseconds for grep-friendly inspection.
/// Both views ship so automation can pick whichever it prefers
/// without re-serializing.
#[derive(Debug, Serialize)]
struct PipelineTimingsReport {
    durations: BTreeMap<String, Duration>,
    durations_ms: BTreeMap<String, u128>,
}

/// Run `$body`, record its wall-clock under `$name` in `$timings`,
/// and return the body's value. Mirrors the in-lowering
/// `time_phase!` macro so the new pipeline phases use the same
/// shape downstream automation already expects.
macro_rules! time_phase {
    ($timings:expr, $name:expr, $body:expr) => {{
        let phase_started = std::time::Instant::now();
        let value = $body;
        $timings.add($name, phase_started.elapsed());
        value
    }};
}

pub fn run_transform_cli(cli: &TransformCli) -> Result<()> {
    let pipeline_started = Instant::now();
    let mut timings = PipelineTimings::default();
    let spec = time_phase!(
        timings,
        "load_spec",
        load_transform_spec_source(&cli.spec_source)
    )?;
    time_phase!(timings, "validate_spec", validate_transform_spec(&spec))?;
    time_phase!(
        timings,
        "preflight_output_roots",
        preflight_output_roots(&spec)
    )?;
    let (artifact, _load_manifest) = time_phase!(
        timings,
        "load_js_chunks",
        load_js_chunks(&spec.inputs.input_root, &spec.inputs.js_list_path)
    )?;
    let materialise_chunk_ids: Vec<String> = spec
        .logical_modules
        .keys()
        .chain(spec.unassigned_mode.keys())
        .chain(spec.chunk_renames.keys())
        .cloned()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let prepare_result = time_phase!(
        timings,
        "prepare_js_chunks",
        prepare_js_chunks(&spec, artifact)
    )?;
    // Aggregate the per-file parse + analyze durations from
    // `prepare_js_chunks` into pipeline-level entries so the cost of
    // parsing vs. shallow analysis is visible without diving into the
    // per-file manifest. The sums dwarf the wall-clock of the
    // surrounding orchestration when parsing is the bottleneck;
    // diverging from it means the orchestration is.
    let mut parse_total = Duration::ZERO;
    let mut analyze_total = Duration::ZERO;
    for record in &prepare_result.parsed_js_files.parsed_files {
        parse_total += record.parse_duration;
        analyze_total += record.analysis_duration;
    }
    timings.add("prepare_js_chunks.parse_files_sum", parse_total);
    timings.add("prepare_js_chunks.analyze_files_sum", analyze_total);
    let artifact_indexes = time_phase!(
        timings,
        "build_artifact_indexes",
        ArtifactIndexes::build(&prepare_result.artifact)
    )?;

    let rewrite_result = time_phase!(
        timings,
        "rewrite_chunk_entry_specifiers",
        rewrite_chunk_entry_specifiers(prepare_result.artifact, &artifact_indexes)
    )?;
    let mut artifact = rewrite_result.artifact;
    let counts = prepare_result.counts;
    let mut chunk_records = prepare_result.chunk_records;
    let mut vendor_report = VendorSwapsReport::default();

    let full_swap_result = time_phase!(
        timings,
        "run_full_vendor_swaps",
        run_full_vendor_swaps(artifact, &artifact_indexes, &spec, cli)
    )?;
    artifact = full_swap_result.artifact;
    vendor_report.full = full_swap_result.full_swap_resolutions;
    chunk_records.retain(|chunk| !full_swap_result.removed_chunk_ids.contains(&chunk.chunk_id));

    let mut module_count: usize = 0;
    let mut selected_lowerings: Vec<artifact::SelectedModuleLowering> = Vec::new();
    let mut decomposition_by_chunk: HashMap<ChunkId, ChunkDecompositionOutput> = HashMap::new();
    if !materialise_chunk_ids.is_empty() {
        let MaterializeLogicalModulesConfig {
            file,
            prune_other_chunks,
            report_out_dir,
            target_dir,
        } = spec.materialize_logical_modules.clone();
        let materialize_result = time_phase!(timings, "materialize_logical_modules", {
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

    let partial_result = time_phase!(
        timings,
        "run_partial_vendor_swaps",
        run_partial_vendor_swaps(artifact, &artifact_indexes, &spec, cli)
    )?;
    artifact = partial_result.artifact;
    vendor_report.partial = partial_result.partial_swap_resolutions;
    vendor_report.bundled_partial = partial_result.bundled_partial_swap_resolutions;
    vendor_report.strip_stats = partial_result.strip_stats;

    time_phase!(
        timings,
        "write_vendor_swaps_report",
        write_vendor_swaps_report(
            spec.swap_vendor_chunks.write,
            spec.swap_vendor_chunks.output_manifest_path.as_deref(),
            &vendor_report,
        )
    )?;

    // Final emit-shape check: every JS file that came out of the
    // materialize / strip pipeline must have unique public export
    // names per file. Catching a duplicate here turns a downstream
    // browser-link failure (which Chromium reports as a silent empty
    // pageerror) into an immediate build-time error pointing at the
    // exact file, name, and source lines. Runs unconditionally so
    // pipelines without vendor swaps still benefit.
    time_phase!(
        timings,
        "validate_emitted_exports",
        validate_emitted_exports(&artifact)
    )?;

    if let Some(cfg) = &spec.write_js_tree {
        time_phase!(
            timings,
            "write_js_tree",
            write_js_tree(&WriteTreeInput {
                artifact: &artifact,
                out_dir: &cfg.out_dir,
                lowerings: &selected_lowerings,
                counts: &counts,
                chunk_records: &chunk_records,
                module_count,
                decomposition_by_chunk: &decomposition_by_chunk,
            })
        )?;
    }

    if let Some(cfg) = &spec.emit_browser_harness {
        let opts = EmitBrowserHarnessOptions {
            asset_summary_path: cfg.asset_summary_path.clone(),
            out_dir: cfg.out_dir.clone(),
            snapshot_root: cfg.snapshot_root.clone(),
        };
        time_phase!(
            timings,
            "emit_browser_harness",
            emit_browser_harness(&artifact, &opts, &chunk_records, &decomposition_by_chunk)
        )?;
    }

    // Persist the pipeline-level breakdown next to the per-chunk
    // `modules.json`. Writing to the same reports root as the rest
    // of the tree means existing automation that mounts the reports
    // directory picks the file up without spec changes; if neither
    // `write_js_tree` nor `emit_browser_harness` was configured (a
    // lib call from a custom harness, say), we silently skip — the
    // numbers can still be re-collected with explicit instrumentation
    // upstream.
    write_pipeline_timings_report(&spec, timings.into_report(pipeline_started.elapsed()))?;

    Ok(())
}

const PIPELINE_REPORT: &str = "pipeline.json";

fn write_pipeline_timings_report(
    spec: &TransformSpec,
    report: PipelineTimingsReport,
) -> Result<()> {
    let out_dir = spec
        .write_js_tree
        .as_ref()
        .map(|cfg| cfg.out_dir.clone())
        .or_else(|| {
            spec.emit_browser_harness
                .as_ref()
                .map(|cfg| cfg.out_dir.clone())
        });
    let Some(out_dir) = out_dir else {
        return Ok(());
    };
    let reports_dir = out_dir.join(REPORTS_DIR);
    fs::create_dir_all(&reports_dir)
        .with_context(|| format!("creating {}", reports_dir.display()))?;
    let path = reports_dir.join(PIPELINE_REPORT);
    write_json(&path, &report)
}

fn load_transform_spec_source(source: &TransformSpecSource) -> Result<TransformSpec> {
    match source {
        TransformSpecSource::Flat { path } => load_flat_transform_spec(path),
        TransformSpecSource::Tree(options) => compile_spec_tree(options),
    }
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

struct FullVendorSwapResult {
    artifact: ChunkBundle,
    full_swap_resolutions: BTreeMap<String, VendorResolution>,
    removed_chunk_ids: BTreeSet<String>,
}

fn run_full_vendor_swaps(
    artifact: ChunkBundle,
    artifact_indexes: &ArtifactIndexes,
    spec: &TransformSpec,
    cli: &TransformCli,
) -> Result<FullVendorSwapResult> {
    let mut artifact = artifact;
    let mut full_swap_resolutions = BTreeMap::new();
    let mut removed_chunk_ids = BTreeSet::new();
    if !spec.vendor.is_empty() {
        apply_vendor_annotations(&artifact, &spec.vendor)?;
        if spec
            .vendor
            .values()
            .any(|m| matches!(m.level, VendorLevel::BoundaryRename | VendorLevel::Swap(_)))
        {
            let rename_result = rename_vendor_exports(artifact, &spec.vendor, artifact_indexes)?;
            artifact = rename_result.artifact;
        }
        if spec
            .vendor
            .values()
            .any(|m| matches!(m.level, VendorLevel::Swap(_)))
        {
            let swap_cfg = &spec.swap_vendor_chunks;
            let swap_result = swap_vendor_chunks(
                artifact,
                &spec.vendor,
                artifact_indexes,
                SwapVendorOptions {
                    package_roots: &cli.package_roots,
                    packages_root: &cli.packages_root,
                    output_manifest_path: swap_cfg.output_manifest_path.clone(),
                    output_wrapper_dir: swap_cfg.output_wrapper_dir.clone(),
                    write: swap_cfg.write,
                },
            )?;
            artifact = swap_result.artifact;
            full_swap_resolutions = swap_result.manifest.resolutions;
            removed_chunk_ids = swap_result.removed_chunk_ids;
        }
    }
    Ok(FullVendorSwapResult {
        artifact,
        full_swap_resolutions,
        removed_chunk_ids,
    })
}

struct PartialVendorSwapResult {
    artifact: ChunkBundle,
    partial_swap_resolutions: BTreeMap<String, ChunkPartialSwapResolution>,
    bundled_partial_swap_resolutions: BTreeMap<String, ChunkBundledPartialSwapResolution>,
    strip_stats: BTreeMap<String, ChunkStripStats>,
}

/// Partial-vendor-swap runs *after* materialize so the per-symbol
/// identifier rewrite (`zodObject(...)` → `z.object(...)`) operates
/// on the already-materialized module files. If it ran before
/// materialize, the rewrite would erase the binding names that
/// `binding_patches` / `logical_modules` selectors still rely on at
/// materialize-time (e.g. `anonymous_statements: match: "ee({...})"`).
fn run_partial_vendor_swaps(
    artifact: ChunkBundle,
    artifact_indexes: &ArtifactIndexes,
    spec: &TransformSpec,
    cli: &TransformCli,
) -> Result<PartialVendorSwapResult> {
    let mut artifact = artifact;
    let mut partial_swap_resolutions = BTreeMap::new();
    let mut bundled_partial_swap_resolutions = BTreeMap::new();
    let mut strip_stats = BTreeMap::new();
    let has_partial_swaps = spec
        .vendor
        .values()
        .any(|m| matches!(m.level, VendorLevel::PartialSwap(_)));
    let has_bundled_partial_swaps = spec
        .vendor
        .values()
        .any(|m| matches!(m.level, VendorLevel::BundledPartialSwap(_)));
    if !spec.vendor.is_empty() && (has_partial_swaps || has_bundled_partial_swaps) {
        let mut replacement_import_locals_by_chunk_path = BTreeMap::new();
        if has_partial_swaps {
            let partial_result = apply_partial_vendor_swaps(
                artifact,
                &spec.vendor,
                artifact_indexes,
                ApplyPartialVendorSwapsOptions {
                    package_roots: &cli.package_roots,
                    packages_root: &cli.packages_root,
                },
            )?;
            artifact = partial_result.artifact;
            partial_swap_resolutions = partial_result.manifest.resolutions;
        }
        if has_bundled_partial_swaps {
            let swap_cfg = &spec.swap_vendor_chunks;
            let bundled_result = apply_bundled_partial_vendor_swaps(
                artifact,
                &spec.vendor,
                artifact_indexes,
                ApplyBundledPartialVendorSwapsOptions {
                    package_roots: &cli.package_roots,
                    packages_root: &cli.packages_root,
                    output_manifest_path: swap_cfg.output_manifest_path.clone(),
                    output_wrapper_dir: swap_cfg.output_wrapper_dir.clone(),
                    write: swap_cfg.write,
                },
            )?;
            artifact = bundled_result.artifact;
            replacement_import_locals_by_chunk_path =
                bundled_result.self_rewrite_import_locals_by_chunk_path;
            bundled_partial_swap_resolutions = bundled_result.manifest.resolutions;
        }
        let strip_result = strip_swapped_vendor_exports_with_options(
            artifact,
            &spec.vendor,
            StripSwappedVendorExportsOptions {
                replacement_import_locals_by_chunk_path,
            },
        )?;
        artifact = strip_result.artifact;
        strip_stats = strip_result.manifest.per_chunk;
    }
    Ok(PartialVendorSwapResult {
        artifact,
        partial_swap_resolutions,
        bundled_partial_swap_resolutions,
        strip_stats,
    })
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
    write_json(path, report)?;
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
    use spec::SwapVendorChunksConfig;
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
                        out_dir: &out,
                        snapshot_root: &snapshot,
                    },
                })?,
            )?;

            run_transform_cli(&TransformCli {
                spec_source: TransformSpecSource::Flat { path: spec_path },
                package_roots: HashMap::new(),
                packages_root: None,
            })?;

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
    fn non_empty_output_dirs_are_rejected() -> Result<()> {
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
            });
            spec.emit_browser_harness = Some(spec::EmitBrowserHarnessConfig {
                asset_summary_path,
                out_dir: harness_out.clone(),
                snapshot_root: snapshot,
            });
            let spec_path = root.join("transform-spec.yaml");
            fs::write(&spec_path, serde_yaml::to_string(&spec)?)?;

            let err = run_transform_cli(&TransformCli {
                spec_source: TransformSpecSource::Flat { path: spec_path },
                package_roots: HashMap::new(),
                packages_root: None,
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
