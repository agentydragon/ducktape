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
use emit_harness::emit_browser_harness;
use lowering::{
    MaterializeLogicalModulesOptions, MaterializeSpecInputs, ReportEmission, UnmatchedSpecClaim,
    materialize_logical_modules,
};
use prepare_chunks::prepare_js_chunks;
use prune_dead_imports::prune_dead_import_specifiers;
use prune_module_exports::prune_unimported_module_exports;
use spec::TransformSpec;
use spec_tree::{CompileSpecTreeOptions, compile_spec_tree};
use validate_emitted_exports::validate_emitted_exports;
use vendor::{
    ChunkBundledPartialSwapResolution, ChunkPartialSwapResolution, ChunkStripStats,
    VendorPlanOptions, VendorResolution, apply_emission_rewrites, build_partial_swap_resolutions,
    build_vendor_resolution_plan, validate_partial_swap_consumers, write_planned_vendor_outputs,
};
use write_tree::{WriteTreeInput, write_js_tree};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TransformCli {
    pub spec_source: TransformSpecSource,
    pub package_roots: HashMap<String, PathBuf>,
    pub packages_root: Option<PathBuf>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TransformRunOptions {
    pub dry_run: bool,
    pub keep_going: bool,
    /// Force per-chunk reports (owner graph, selector diagnostics, …) to this
    /// directory, overriding the spec's `materialize_logical_modules.report_out_dir`.
    /// Used by `debundle spec validate --keep-going` to capture the keep-going
    /// selector diagnostics regardless of how the spec configures reporting.
    pub report_dir_override: Option<PathBuf>,
}

impl Default for TransformRunOptions {
    fn default() -> Self {
        Self {
            dry_run: false,
            keep_going: true,
            report_dir_override: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TransformSpecSource {
    Flat { path: PathBuf },
    Tree(CompileSpecTreeOptions),
}

/// Command-line arguments for the debundle transform pipeline.
#[derive(ClapArgs, Debug)]
pub struct TransformArgs {
    /// Path to a flat transform spec YAML. Pass exactly one of `--spec`
    /// / `--tree-config`.
    #[arg(long)]
    pub spec: Option<PathBuf>,
    /// Path to a tree-shaped authoring config YAML. Requires
    /// `--tree-modules`, `--tree-vendor-marks`, and `--out-root`.
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
    /// Run pipeline parse/facts/gate checks without writing emitted JS
    /// or accept-path reports. A gate rejection still writes
    /// `owner_graph.json` plus the rejection evidence (`cycles.json` /
    /// `atomic_unit_conflicts.json`) under `reports/tree/<chunk>/`, so
    /// `gate list` / `gate describe` work on the rejection just
    /// reported.
    #[arg(long)]
    pub dry_run: bool,
    /// Deprecated compatibility no-op: keep-going is now the default.
    /// Use `--fail-fast` to stop at the first supported diagnostic instead.
    #[arg(long)]
    pub keep_going: bool,
    /// Stop at the first supported diagnostic instead of collecting all
    /// findings from the pass. Broad runs otherwise aggregate every
    /// supported diagnostic — currently unresolved source-match
    /// selectors and duplicate binding claims, with
    /// module/export/origin evidence — before failing.
    #[arg(long, conflicts_with = "keep_going")]
    pub fail_fast: bool,
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
    let chunk_records = prepare_result.chunk_records;
    let mut vendor_report = VendorSwapsReport::default();

    let mut indexed = IndexedArtifact::new(prepare_result.artifact)?;

    // Single post-prepare vendor resolution pass: validates every mark,
    // resolves boundary mappings, swap targets, and partial-swap symbol
    // tables once, and runs the plan-time consumer gate and the
    // full-swap caller import-alignment check before anything is written. Vendor
    // application below consumes this plan instead of re-resolving the
    // spec mid-pipeline.
    let swap_cfg = &spec.swap_vendor_chunks;
    let vendor_plan = build_vendor_resolution_plan(
        indexed.artifact(),
        indexed.indexes(),
        &spec.vendor,
        &VendorPlanOptions {
            package_roots: &cli.package_roots,
            packages_root: &cli.packages_root,
            output_manifest_path: swap_cfg.output_manifest_path.as_deref(),
            output_wrapper_dir: swap_cfg.output_wrapper_dir.as_deref(),
        },
    )?;
    let write_vendor_outputs = swap_cfg.write && !options.dry_run;

    // Full swaps are an emission-set exclusion: the swapped chunks stay in
    // the bundle — nothing removes them, and the owner graph never
    // contained vendor chunks — but emission, the rename queue, the
    // emit-shape check, and the chunk reports skip them. Their
    // wire-facing resolutions are projections of the plan.
    let excluded_chunk_ids = vendor_plan.full_swap_chunk_ids();
    vendor_report.full = vendor_plan.full_swap_resolutions();

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
    // Vendor rewrites lowering applied at construction time inside
    // materialized module bodies, keyed by (swapped chunk, chunk
    // export); folded into the partial-swap manifests below so
    // `references_rewritten` keeps counting emitted references across
    // both application sites.
    let mut vendor_lowering_rewrites: BTreeMap<(ChunkId, String), usize> = BTreeMap::new();
    if !materialise_chunk_ids.is_empty() {
        let report_out_dir = spec.materialize_logical_modules.report_out_dir.clone();
        // `validate --keep-going` forces reports to its own capture dir even
        // when the spec leaves `report_out_dir` unset; otherwise honor the spec.
        let report_out_dir = options.report_dir_override.clone().or(report_out_dir);
        // `materialize_logical_modules` derives its own per-run indexes
        // internally (it prunes chunks first); the pipeline indexes are
        // not consumed here, but the artifact mutates, so the update
        // rebuilds them for the emission rewrites below.
        (indexed, _) = indexed.update(|artifact, _indexes| {
            let materialize_result = materialize_logical_modules(
                artifact,
                MaterializeSpecInputs {
                    logical_modules: &spec.logical_modules,
                    chunk_renames: &spec.chunk_renames,
                    unassigned_mode: &spec.unassigned_mode,
                    chunk_analysis_options: &spec.chunk_analysis_options,
                    chunk_export_purity: &spec.chunk_export_purity,
                },
                &vendor_plan,
                MaterializeLogicalModulesOptions {
                    config: spec.materialize_logical_modules.clone(),
                    chunk_ids: materialise_chunk_ids,
                    keep_going: options.keep_going,
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
                },
            )?;
            module_count = materialize_result.module_count;
            selected_lowerings = materialize_result.selected_lowerings;
            decomposition_by_chunk = materialize_result.decomposition_by_chunk;
            unmatched_spec_claims = materialize_result.unmatched_spec_claims;
            vendor_lowering_rewrites = materialize_result.vendor_reference_rewrites;
            Ok((materialize_result.artifact, ()))
        })?;
    }

    // Emission rewrites, one artifact pass over two disjoint file sets:
    //
    // * the unified pass-through directive rewrite over files emitted
    //   without lowering in non-vendor chunks — specifier
    //   canonicalization (the old always-on stage 0), boundary-rename
    //   name mapping, and partial-swap consumer surgery from the
    //   vendor plan (materialized module bodies were constructed from
    //   the same plan during lowering; suppress-marked chunks are
    //   skipped wholesale per open question 3; excluded full-swap
    //   chunks are never rewritten);
    // * the vendor residual computation for each partially-swapped
    //   chunk — the canonicalize → self-rewrite → strip composition as
    //   one function body, so the former stage ordering is structural
    //   instead of a pipeline concern.
    //
    // Runs after materialize so the per-symbol consumer rewrite cannot
    // erase binding names spec selectors matched on.
    let mut vendor_rewrite_counts = vendor_lowering_rewrites;
    let emission_outcome;
    (indexed, emission_outcome) = indexed.update(|artifact, indexes| {
        let emission_result = apply_emission_rewrites(artifact, &vendor_plan, indexes)?;
        Ok((
            emission_result.artifact,
            (
                emission_result.references_by_symbol,
                emission_result.strip_stats,
            ),
        ))
    })?;
    let (emission_references, strip_stats) = emission_outcome;
    merge_rewrite_counts(&mut vendor_rewrite_counts, emission_references);
    vendor_report.strip_stats = strip_stats;

    if vendor_plan.has_partial_swaps() || vendor_plan.has_bundled_partial_swaps() {
        // Post-strip consumer gate: no retained file may still consume
        // the stripped portion of a partially-swapped chunk's export
        // surface. Load-bearing behind the plan-time gate — it covers
        // directives lowering synthesized inside materialized module
        // bodies (see `validate_partial_swap_consumers`).
        validate_partial_swap_consumers(indexed.artifact(), &vendor_plan, indexed.indexes())?;
    }
    (vendor_report.partial, vendor_report.bundled_partial) =
        build_partial_swap_resolutions(&vendor_plan, &vendor_rewrite_counts)?;
    let mut artifact = indexed.into_artifact();

    // Vendor emission outputs: full-swap wrappers and bundled bundle
    // copies / facades, plus the combined manifest — all write-gated
    // behind `swap_vendor_chunks.write`.
    if write_vendor_outputs {
        write_planned_vendor_outputs(&vendor_plan)?;
    }
    write_vendor_swaps_report(
        write_vendor_outputs,
        spec.swap_vendor_chunks.output_manifest_path.as_deref(),
        &vendor_report,
    )?;

    // Trim dead named import specifiers across the whole bundle (entry
    // included): the lowerer over-imports residual/hub bindings the
    // importing file never references. Runs immediately before the
    // export prune so that dropping a dead import — which the
    // name-based export prune would otherwise treat as a live consumer —
    // cascades into dropping the now-unimported module exports. See
    // prune_dead_imports.rs for the soundness argument.
    prune_dead_import_specifiers(&mut artifact);

    // Drop dead named exports from emitted logical-module files: a
    // module-owned binding referenced nowhere outside its own module
    // (the esbuild decorator scaffolding is the dominant case) is
    // module-internal, so it should not appear in the module's
    // `export { ... }`. Runs over the final bundle so it sees every
    // import/re-export; entry files (the chunk public surface) are
    // never touched. See prune_module_exports.rs for the soundness
    // argument.
    prune_unimported_module_exports(&mut artifact);

    // Final emit-shape check: every JS file that came out of the
    // materialize / strip pipeline must have unique public export
    // names per file. Catching a duplicate here turns a downstream
    // browser-link failure (which Chromium reports as a silent empty
    // pageerror) into an immediate build-time error pointing at the
    // exact file, name, and source lines. Runs unconditionally so
    // pipelines without vendor swaps still benefit.
    validate_emitted_exports(&artifact, &excluded_chunk_ids)?;

    // Chunk records of the emission set: records of excluded
    // (fully-swapped) chunks are dropped from the emitted reports.
    let excluded_chunk_names: BTreeSet<&str> = excluded_chunk_ids
        .iter()
        .map(|chunk_id| artifact.chunk_table.name(*chunk_id))
        .collect();
    let emitted_chunk_records: Vec<_> = chunk_records
        .into_iter()
        .filter(|record| !excluded_chunk_names.contains(record.chunk_id.as_str()))
        .collect();

    if !options.dry_run
        && let Some(cfg) = &spec.write_js_tree
    {
        write_js_tree(&WriteTreeInput {
            artifact: &artifact,
            out_dir: &cfg.out_dir,
            lowerings: &selected_lowerings,
            counts: &counts,
            chunk_records: &emitted_chunk_records,
            module_count,
            decomposition_by_chunk: &decomposition_by_chunk,
            excluded_chunk_ids: &excluded_chunk_ids,
        })?;
    }

    if !options.dry_run
        && let Some(cfg) = &spec.emit_browser_harness
    {
        emit_browser_harness(
            &artifact,
            cfg,
            &emitted_chunk_records,
            &decomposition_by_chunk,
            &excluded_chunk_ids,
        )?;
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

/// Fold one application site's per-symbol rewrite counts into the
/// merged map `build_partial_swap_resolutions` projects from.
fn merge_rewrite_counts(
    merged: &mut BTreeMap<(ChunkId, String), usize>,
    counts: BTreeMap<(ChunkId, String), usize>,
) {
    for (key, count) in counts {
        *merged.entry(key).or_insert(0) += count;
    }
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
    fn tree_authoring_resolves_source_selectors_in_each_mapped_chunk() -> Result<()> {
        js_ast::with_swc_globals(|| {
            let temp = tempfile::tempdir()?;
            let root = temp.path();
            let snapshot = root.join("snapshot");
            let extracted = root.join("extracted");
            let modules = root.join("modules");
            let out = root.join("out");
            fs::create_dir_all(&snapshot)?;
            fs::create_dir_all(&extracted)?;
            fs::create_dir_all(modules.join("chunks/cli/runtime"))?;
            fs::create_dir_all(modules.join("chunks/print/protocol"))?;
            fs::write(
                snapshot.join("cli.js"),
                "import { printFeature } from './print.js';\nfunction cliFeature() { return `cli:${printFeature()}`; }\nconsole.log(cliFeature());\nexport { cliFeature };\n",
            )?;
            fs::write(
                snapshot.join("print.js"),
                "function printFeature() { return 'print'; }\nexport { printFeature };\n",
            )?;
            fs::write(extracted.join("js-files.txt"), "cli.js\nprint.js\n")?;
            fs::write(
                modules.join("chunks/cli/runtime/session.yaml"),
                r#"source_matches:
  - match: |
      function selectedFeature() {
        return `cli:${printFeature()}`;
      }
    bindings:
      - local: selectedFeature
        name: CliFeature
"#,
            )?;
            fs::write(
                modules.join("chunks/print/protocol/stream.yaml"),
                r#"source_matches:
  - match: |
      function selectedFeature() {
        return "print";
      }
    bindings:
      - local: selectedFeature
        name: PrintFeature
"#,
            )?;
            let config = root.join("spec_config.yaml");
            fs::write(
                &config,
                r#"main_chunk_id: cli
module_roots:
  cli: chunks/cli
  print: chunks/print
inputs:
  root: snapshot
  js_list_path: extracted/js-files.txt
write_js_tree: true
unassigned_mode:
  cli: { kind: inline_in_entry }
  print: { kind: inline_in_entry }
"#,
            )?;
            let vendor_marks = root.join("vendor_marks.yaml");
            fs::write(&vendor_marks, "vendor_marks: []\n")?;

            run_transform_cli(&TransformCli {
                spec_source: TransformSpecSource::Tree(CompileSpecTreeOptions {
                    config_path: config,
                    modules_root: modules,
                    vendor_marks_path: vendor_marks,
                    source_root: Some(root.to_path_buf()),
                    out_root: out.clone(),
                }),
                package_roots: HashMap::new(),
                packages_root: None,
            })?;

            let cli_module = fs::read_to_string(out.join("app/cli/runtime/session.js"))?;
            let print_module = fs::read_to_string(out.join("app/print/protocol/stream.js"))?;
            assert!(cli_module.contains("CliFeature"), "{cli_module}");
            assert!(cli_module.contains("printFeature"), "{cli_module}");
            assert!(cli_module.contains("../../print/entry.js"), "{cli_module}");
            assert!(!cli_module.contains("PrintFeature"), "{cli_module}");
            assert!(print_module.contains("PrintFeature"), "{print_module}");
            assert!(print_module.contains("'print'"), "{print_module}");
            assert!(!print_module.contains("CliFeature"), "{print_module}");
            let print_entry = fs::read_to_string(out.join("app/print/entry.js"))?;
            assert!(print_entry.contains("PrintFeature"), "{print_entry}");
            assert!(print_entry.contains("printFeature"), "{print_entry}");
            assert!(out.join("reports/tree/cli/modules.json").exists());
            assert!(out.join("reports/tree/print/modules.json").exists());
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
                TransformRunOptions {
                    dry_run: true,
                    keep_going: false,
                    report_dir_override: None,
                },
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
            materialize_logical_modules: spec::MaterializeLogicalModulesConfig::default(),
            write_js_tree: None,
            emit_browser_harness: None,
        }
    }
}
