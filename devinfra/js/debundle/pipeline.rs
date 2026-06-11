use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use clap::Args as ClapArgs;
use serde::Serialize;

use artifact::IndexedArtifact;
use artifact::load_js_chunks;
use artifact::write_json;
use artifact::{ChunkDecompositionOutput, ChunkId};
use emit_harness::{EmitBrowserHarnessOptions, emit_browser_harness};
use lowering::{
    MaterializeLogicalModulesOptions, ReportEmission, UnmatchedSpecClaim,
    materialize_logical_modules,
};
use prepare_chunks::prepare_js_chunks;
use rewrite_specifiers::rewrite_chunk_entry_specifiers;
use spec::{MaterializeLogicalModulesConfig, TransformSpec};
use spec_tree::{CompileSpecTreeOptions, compile_spec_tree};
use validate_emitted_exports::validate_emitted_exports;
use vendor::{
    ChunkBundledPartialSwapResolution, ChunkPartialSwapResolution, ChunkStripStats,
    StripSwappedVendorExportsOptions, VendorPlanOptions, VendorResolution, VendorResolutionPlan,
    apply_bundled_partial_vendor_swaps, apply_partial_vendor_swaps, build_vendor_resolution_plan,
    rename_vendor_exports, strip_swapped_vendor_exports_with_options, swap_vendor_chunks,
    validate_partial_swap_consumers,
};
use write_tree::{WriteTreeInput, write_js_tree};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TransformCli {
    pub spec_source: TransformSpecSource,
    pub package_roots: HashMap<String, PathBuf>,
    pub packages_root: Option<PathBuf>,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct TransformRunOptions {
    pub dry_run: bool,
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
    /// Deliberately NOT `DEBUNDLE_SOURCE_ROOT`: that env var is the query
    /// commands' upstream-snapshot root, a different directory in real corpora.
    #[arg(long = "tree-source-root", env = "DEBUNDLE_TREE_SOURCE_ROOT")]
    pub tree_source_root: Option<PathBuf>,
    /// Output root used when compiling tree-shaped authoring sources.
    #[arg(long = "out-root", env = "DEBUNDLE_OUT")]
    pub out_root: Option<PathBuf>,
    /// Map a package name to its source directory: `<pkg>=<dir>`. May be repeated.
    #[arg(long = "package-root", value_parser = parse_package_root_kv)]
    pub package_roots: Vec<(String, PathBuf)>,
    /// Root directory containing per-package sources (alternative to repeated --package-root).
    #[arg(long)]
    pub packages_root: Option<PathBuf>,
    /// Run pipeline checks without writing emitted JS or reports.
    #[arg(long)]
    pub dry_run: bool,
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

pub fn run_transform_cli(cli: &TransformCli) -> Result<()> {
    run_transform_cli_with_options(cli, TransformRunOptions::default())
}

pub fn run_transform_cli_with_options(
    cli: &TransformCli,
    options: TransformRunOptions,
) -> Result<()> {
    let spec = load_transform_spec_source(&cli.spec_source)?;
    validate_transform_spec(&spec)?;
    if !options.dry_run {
        preflight_output_roots(&spec)?;
    }
    let (artifact, _load_manifest) =
        load_js_chunks(&spec.inputs.input_root, &spec.inputs.js_list_path)?;
    let materialise_chunk_ids: Vec<String> = spec
        .logical_modules
        .keys()
        .chain(spec.unassigned_mode.keys())
        .chain(spec.chunk_renames.keys())
        .cloned()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let prepare_result = prepare_js_chunks(&spec, artifact)?;
    let counts = prepare_result.counts;
    let mut chunk_records = prepare_result.chunk_records;
    let mut vendor_report = VendorSwapsReport::default();

    let mut indexed = IndexedArtifact::new(prepare_result.artifact)?;
    (indexed, _) = indexed.update(|artifact, indexes| {
        let rewrite_result = rewrite_chunk_entry_specifiers(artifact, indexes)?;
        Ok((rewrite_result.artifact, ()))
    })?;

    // Single post-prepare vendor resolution pass: validates every mark
    // and resolves boundary mappings, swap targets, and partial-swap
    // symbol tables once. The vendor waves below consume this plan
    // instead of re-resolving the spec mid-pipeline.
    let swap_cfg = &spec.swap_vendor_chunks;
    let vendor_plan = build_vendor_resolution_plan(
        indexed.artifact(),
        &spec.vendor,
        &VendorPlanOptions {
            package_roots: &cli.package_roots,
            packages_root: &cli.packages_root,
            output_manifest_path: swap_cfg.output_manifest_path.as_deref(),
            output_wrapper_dir: swap_cfg.output_wrapper_dir.as_deref(),
        },
    )?;
    let write_vendor_outputs = swap_cfg.write && !options.dry_run;

    let full_swap_outcome;
    (indexed, full_swap_outcome) =
        run_full_vendor_swaps(indexed, &vendor_plan, write_vendor_outputs)?;
    vendor_report.full = full_swap_outcome.full_swap_resolutions;
    chunk_records.retain(|chunk| {
        !full_swap_outcome
            .removed_chunk_ids
            .contains(&chunk.chunk_id)
    });

    let mut module_count: usize = 0;
    let mut selected_lowerings: Vec<artifact::SelectedModuleLowering> = Vec::new();
    let mut decomposition_by_chunk: HashMap<ChunkId, ChunkDecompositionOutput> = HashMap::new();
    // Spec member claims whose `binding.name` did not resolve to a
    // top-level declaration in the source chunk. Collected across
    // every chunk by `materialize_logical_modules`; surfaced at the
    // end of the pipeline (after vendor swaps, emit-shape check, and
    // tree/harness emission) so the user sees the full list and the
    // generated output is still available for inspection.
    let mut unmatched_spec_claims: Vec<UnmatchedSpecClaim> = Vec::new();
    if !materialise_chunk_ids.is_empty() {
        let MaterializeLogicalModulesConfig {
            file,
            prune_other_chunks,
            report_out_dir,
            target_dir,
        } = spec.materialize_logical_modules.clone();
        // `materialize_logical_modules` derives its own per-run indexes
        // internally (it prunes chunks first); the pipeline indexes are
        // not consumed here, but the artifact mutates, so the update
        // rebuilds them for the partial-swap stage below.
        (indexed, _) = indexed.update(|artifact, _indexes| {
            let materialize_result = materialize_logical_modules(
                artifact,
                &spec.logical_modules,
                &spec.chunk_renames,
                &spec.unassigned_mode,
                &spec.chunk_analysis_options,
                &spec.chunk_export_purity,
                MaterializeLogicalModulesOptions {
                    chunk_ids: materialise_chunk_ids,
                    file,
                    prune_other_chunks,
                    // Dry-run keeps the no-output contract on the
                    // accept path but still materializes rejection
                    // evidence (owner graph + cycles/conflicts) at the
                    // same reports/tree/<chunk>/ location a real run
                    // uses, so `debundle gate list/describe` works on
                    // the rejection that was just reported.
                    report_emission: match report_out_dir {
                        Some(dir) if options.dry_run => ReportEmission::OnRejection(dir),
                        Some(dir) => ReportEmission::Full(dir),
                        None => ReportEmission::None,
                    },
                    target_dir,
                },
            )?;
            module_count = materialize_result.module_count;
            selected_lowerings = materialize_result.selected_lowerings;
            decomposition_by_chunk = materialize_result.decomposition_by_chunk;
            unmatched_spec_claims = materialize_result.unmatched_spec_claims;
            Ok((materialize_result.artifact, ()))
        })?;
    }

    let partial_outcome;
    (indexed, partial_outcome) =
        run_partial_vendor_swaps(indexed, &vendor_plan, write_vendor_outputs)?;
    vendor_report.partial = partial_outcome.partial_swap_resolutions;
    vendor_report.bundled_partial = partial_outcome.bundled_partial_swap_resolutions;
    vendor_report.strip_stats = partial_outcome.strip_stats;
    let artifact = indexed.into_artifact();

    write_vendor_swaps_report(
        spec.swap_vendor_chunks.write && !options.dry_run,
        spec.swap_vendor_chunks.output_manifest_path.as_deref(),
        &vendor_report,
    )?;

    // Final emit-shape check: every JS file that came out of the
    // materialize / strip pipeline must have unique public export
    // names per file. Catching a duplicate here turns a downstream
    // browser-link failure (which Chromium reports as a silent empty
    // pageerror) into an immediate build-time error pointing at the
    // exact file, name, and source lines. Runs unconditionally so
    // pipelines without vendor swaps still benefit.
    validate_emitted_exports(&artifact)?;

    if !options.dry_run
        && let Some(cfg) = &spec.write_js_tree
    {
        write_js_tree(&WriteTreeInput {
            artifact: &artifact,
            out_dir: &cfg.out_dir,
            lowerings: &selected_lowerings,
            counts: &counts,
            chunk_records: &chunk_records,
            module_count,
            decomposition_by_chunk: &decomposition_by_chunk,
        })?;
    }

    if !options.dry_run
        && let Some(cfg) = &spec.emit_browser_harness
    {
        let opts = EmitBrowserHarnessOptions {
            asset_summary_path: cfg.asset_summary_path.clone(),
            out_dir: cfg.out_dir.clone(),
            snapshot_root: cfg.snapshot_root.clone(),
        };
        emit_browser_harness(&artifact, &opts, &chunk_records, &decomposition_by_chunk)?;
    }

    // Defer unmatched-spec-claim failure to here so the build runs
    // emit + reports first — the user gets the full list across
    // every chunk plus the artifact tree to inspect, instead of one
    // bail per first-offending chunk and no output.
    if !unmatched_spec_claims.is_empty() {
        let mut lines = String::new();
        for claim in &unmatched_spec_claims {
            lines.push_str(&format!(
                "  - chunk {chunk_id}: module {module_path} claims binding `{binding}` (export `{export}`); \
                 no top-level declaration with that name exists in the chunk.\n",
                chunk_id = claim.chunk_id,
                module_path = claim.module_path,
                binding = claim.binding_name,
                export = claim.export_name,
            ));
        }
        bail!(
            "Transform spec referenced {n} top-level binding name(s) that the source chunk \
             does not declare. The pipeline finished emitting (so any unrelated modules \
             still landed), but unresolved spec claims silently fall into the residual sweep \
             and leave the named destination module short an export. Fix the spec (correct \
             the binding name, drop the member, or rename via `chunk_renames`):\n{lines}",
            n = unmatched_spec_claims.len(),
        );
    }

    Ok(())
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

struct FullVendorSwapOutcome {
    full_swap_resolutions: BTreeMap<String, VendorResolution>,
    removed_chunk_ids: BTreeSet<String>,
}

fn run_full_vendor_swaps(
    mut indexed: IndexedArtifact,
    plan: &VendorResolutionPlan,
    write_outputs: bool,
) -> Result<(IndexedArtifact, FullVendorSwapOutcome)> {
    let mut full_swap_resolutions = BTreeMap::new();
    let mut removed_chunk_ids = BTreeSet::new();
    if plan.has_boundary_renames() {
        (indexed, _) = indexed.update(|artifact, indexes| {
            let rename_result = rename_vendor_exports(artifact, plan, indexes)?;
            Ok((rename_result.artifact, ()))
        })?;
    }
    if plan.has_full_swaps() {
        (indexed, (full_swap_resolutions, removed_chunk_ids)) =
            indexed.update(|artifact, indexes| {
                let swap_result = swap_vendor_chunks(artifact, plan, indexes, write_outputs)?;
                Ok((
                    swap_result.artifact,
                    (
                        swap_result.manifest.resolutions,
                        swap_result.removed_chunk_ids,
                    ),
                ))
            })?;
    }
    Ok((
        indexed,
        FullVendorSwapOutcome {
            full_swap_resolutions,
            removed_chunk_ids,
        },
    ))
}

struct PartialVendorSwapOutcome {
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
    mut indexed: IndexedArtifact,
    plan: &VendorResolutionPlan,
    write_outputs: bool,
) -> Result<(IndexedArtifact, PartialVendorSwapOutcome)> {
    let mut partial_swap_resolutions = BTreeMap::new();
    let mut bundled_partial_swap_resolutions = BTreeMap::new();
    let mut strip_stats = BTreeMap::new();
    if plan.has_partial_swaps() || plan.has_bundled_partial_swaps() {
        let mut replacement_import_locals_by_chunk_path = BTreeMap::new();
        if plan.has_partial_swaps() {
            (indexed, partial_swap_resolutions) = indexed.update(|artifact, indexes| {
                let partial_result = apply_partial_vendor_swaps(artifact, plan, indexes)?;
                Ok((partial_result.artifact, partial_result.manifest.resolutions))
            })?;
        }
        if plan.has_bundled_partial_swaps() {
            (
                indexed,
                (
                    replacement_import_locals_by_chunk_path,
                    bundled_partial_swap_resolutions,
                ),
            ) = indexed.update(|artifact, indexes| {
                let bundled_result =
                    apply_bundled_partial_vendor_swaps(artifact, plan, indexes, write_outputs)?;
                Ok((
                    bundled_result.artifact,
                    (
                        bundled_result.self_rewrite_import_locals_by_chunk_path,
                        bundled_result.manifest.resolutions,
                    ),
                ))
            })?;
        }
        (indexed, strip_stats) = indexed.update(|artifact, _indexes| {
            let strip_result = strip_swapped_vendor_exports_with_options(
                artifact,
                plan,
                StripSwappedVendorExportsOptions {
                    replacement_import_locals_by_chunk_path,
                },
            )?;
            Ok((strip_result.artifact, strip_result.manifest.per_chunk))
        })?;
        // Post-strip soundness gate: no retained file may still consume
        // the stripped portion of a partially-swapped chunk's export
        // surface (unrewritten named imports / re-exports, namespace
        // imports, `export *`).
        validate_partial_swap_consumers(indexed.artifact(), plan, indexed.indexes())?;
    }
    Ok((
        indexed,
        PartialVendorSwapOutcome {
            partial_swap_resolutions,
            bundled_partial_swap_resolutions,
            strip_stats,
        },
    ))
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
    <script type="module" src="./static/index-EXAMPLE.js"></script>
  </body>
</html>
"#,
            )?;
            fs::write(
                snapshot.join("static/index-EXAMPLE.js"),
                "import { y } from './chunk-DuckMock.js';\nglobalThis.__value = y;\n",
            )?;
            fs::write(
                snapshot.join("static/chunk-DuckMock.js"),
                "export const y = 2;\n",
            )?;
            fs::write(
                extracted.join("js-files.txt"),
                "static/index-EXAMPLE.js\nstatic/chunk-DuckMock.js\n",
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
            let entry = fs::read_to_string(out.join("app/static/index-EXAMPLE/entry.js"))?;
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

    #[test]
    fn dry_run_runs_pipeline_checks_without_writing_outputs() -> Result<()> {
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

            run_transform_cli_with_options(
                &TransformCli {
                    spec_source: TransformSpecSource::Flat { path: spec_path },
                    package_roots: HashMap::new(),
                    packages_root: None,
                },
                TransformRunOptions { dry_run: true },
            )?;

            assert!(js_out.join("stale.txt").exists());
            assert!(harness_out.join("stale.txt").exists());
            assert!(!js_out.join("app").exists());
            assert!(!harness_out.join("app").exists());
            assert!(!js_out.join("reports").exists());
            assert!(!harness_out.join("reports").exists());
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
            chunk_export_purity: BTreeMap::new(),
            swap_vendor_chunks: SwapVendorChunksConfig::default(),
            materialize_logical_modules: MaterializeLogicalModulesConfig::default(),
            write_js_tree: None,
            emit_browser_harness: None,
        }
    }
}
