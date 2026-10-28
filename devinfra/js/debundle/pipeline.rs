use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use anyhow::{Context, Result, bail};
use clap::Args as ClapArgs;

use artifact::ChunkId;
use artifact::load_js_chunks;
use artifact::reports::ChunkDecompositionOutput;
use emit_harness::emit_browser_harness;
use graph_provenance::{CurrentRoots, GraphProvenance, InputCapture, ProvenanceRoots};
use lowering::{
    MaterializeLogicalModulesOptions, MaterializeSpecInputs, ReportEmission,
    materialize_logical_modules, naturalize_cross_chunk_imports,
};
use prepare_chunks::{SourcePreparation, prepare_js_chunks};
use prune_dead_imports::prune_dead_import_specifiers;
use prune_module_exports::prune_unimported_module_exports;
use reconstruction::{ReconstructionPlan, UnmatchedSpecClaim, VendorManifestOutput};
use spec::{ChunkExportPurity, TransformSpec};
use spec_modules::read_flat_transform_spec;
use spec_tree::{CompileSpecTreeOptions, compile_spec_tree, graph_module_roots};
use vendor::{
    VendorPlanOptions, VendorResolutionPlan, VendorSwapsReport, apply_emission_rewrites_in_place,
    build_partial_swap_resolutions, build_vendor_resolution_plan,
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
    /// Force per-chunk reports (dependency graph, selector diagnostics, …) to this
    /// directory, overriding the spec's `materialize_logical_modules.report_out_dir`.
    /// Used by `debundle spec validate` to capture the keep-going
    /// selector diagnostics regardless of how the spec configures reporting.
    pub report_dir_override: Option<PathBuf>,
    /// Add what each matched template's free identifiers mean to every
    /// chunk's selector report, and write the report even when every
    /// selector resolved. For `debundle spec validate`.
    pub list_template_identifiers: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TransformSpecSource {
    Flat { path: PathBuf },
    Tree(CompileSpecTreeOptions),
}

/// Command-line arguments for the debundle transform pipeline.
#[derive(ClapArgs, Debug, Clone)]
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
    /// `dependency_graph.json` plus the rejection evidence (`cycles.json` /
    /// `atomic_group_conflicts.json`) under `reports/tree/<chunk>/`, so
    /// `gate list` / `gate describe` work on the rejection just
    /// reported.
    #[arg(long)]
    pub dry_run: bool,
    /// Stop at the first supported diagnostic instead of collecting all
    /// findings from the pass. Broad runs otherwise aggregate every
    /// supported diagnostic — currently unresolved source-match
    /// selectors and duplicate binding claims, with
    /// module/export/origin evidence — before failing.
    #[arg(long)]
    pub fail_fast: bool,
}

impl TransformArgs {
    /// Collapse `--package-root` pairs into a `HashMap` and validate spec source.
    pub fn resolve(self) -> Result<TransformCli> {
        let spec_source = resolve_spec_source(&self)?;
        Ok(TransformCli {
            spec_source,
            package_roots: collect_package_roots(self.package_roots)?
                .into_iter()
                .collect(),
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

pub fn parse_package_root_kv(value: &str) -> Result<(String, PathBuf), String> {
    let Some(separator) = value.find('=') else {
        return Err(format!(
            "package root mapping must be <package>=<dir>, got {value}"
        ));
    };
    if separator == 0 || separator == value.len() - 1 {
        return Err(format!(
            "package root mapping must be <package>=<dir>, got {value}"
        ));
    }
    Ok((
        value[..separator].to_string(),
        PathBuf::from(&value[separator + 1..]),
    ))
}

pub fn collect_package_roots(
    pairs: impl IntoIterator<Item = (String, PathBuf)>,
) -> Result<std::collections::BTreeMap<String, PathBuf>> {
    let mut roots = std::collections::BTreeMap::new();
    for (name, path) in pairs {
        anyhow::ensure!(
            roots.insert(name.clone(), path).is_none(),
            "duplicate package root mapping for {name}"
        );
    }
    Ok(roots)
}

pub fn run_transform_cli(cli: &TransformCli, options: TransformRunOptions) -> Result<()> {
    let (spec, prepared) = prepare_transform(cli, &options, None)?;
    run_prepared_transform(&spec, prepared, options, false)
}

/// Input-only preparation shared by the baseline and independent contract probes.
/// Each run gets its own mutable AST; vendor decisions remain read-only.
#[derive(Clone)]
struct PreparedTransform {
    source: SourcePreparation,
    vendor_plan: Arc<VendorResolutionPlan>,
    provenance: GraphProvenance,
}

/// One accepted baseline and its immutable input preparation. Only the contract
/// map may change between probes: other spec fields affect preparation.
pub struct ContractAudit {
    spec: TransformSpec,
    prepared: PreparedTransform,
}

#[derive(Debug)]
pub enum ContractProbeOutcome {
    Accepted,
    Rejected(anyhow::Error),
    Inconclusive(anyhow::Error),
}

impl ContractAudit {
    pub fn prepare(cli: &TransformCli, expected_spec: &TransformSpec) -> Result<Self> {
        let (spec, prepared) = prepare_transform(cli, &audit_options(), Some(expected_spec))?;
        run_prepared_transform(&spec, prepared.clone(), audit_options(), true)?;
        prepared
            .provenance
            .verify(&CurrentRoots::default())
            .context("audit inputs changed during baseline")?;
        Ok(Self { spec, prepared })
    }

    pub fn provenance(&self) -> &GraphProvenance {
        &self.prepared.provenance
    }

    /// Recompute all contract-dependent analysis and full reconstruction
    /// acceptance. Input-verification failures abort the audit, including when
    /// analysis rejects; they must never become a cached contract verdict.
    /// No graph or output artifacts may escape an edited probe.
    pub fn probe(
        &self,
        contracts: BTreeMap<String, ChunkExportPurity>,
    ) -> Result<ContractProbeOutcome> {
        self.provenance()
            .verify(&CurrentRoots::default())
            .context("audit inputs changed after baseline")?;
        let mut spec = self.spec.clone();
        spec.chunk_export_purity = contracts;
        if let Err(error) = validate_transform_spec(&spec) {
            return Ok(ContractProbeOutcome::Inconclusive(error));
        }
        let outcome = run_prepared_transform(&spec, self.prepared.clone(), audit_options(), true);
        self.provenance()
            .verify(&CurrentRoots::default())
            .context("audit inputs changed between baseline and probe")?;
        Ok(match outcome {
            Ok(()) => ContractProbeOutcome::Accepted,
            Err(error) if error.chain().any(|cause| cause.is::<std::io::Error>()) => {
                ContractProbeOutcome::Inconclusive(error)
            }
            Err(error) => ContractProbeOutcome::Rejected(error),
        })
    }
}

fn audit_options() -> TransformRunOptions {
    TransformRunOptions {
        dry_run: true,
        keep_going: false,
        report_dir_override: None,
        list_template_identifiers: false,
    }
}

fn prepare_transform(
    cli: &TransformCli,
    options: &TransformRunOptions,
    expected_spec: Option<&TransformSpec>,
) -> Result<(TransformSpec, PreparedTransform)> {
    let (roots, modules_directory) = provenance_roots(cli)?;
    let generation_command = canonical_generation_command(cli, options, &roots)?;
    let capture = InputCapture::start(roots, modules_directory, generation_command)?;
    if let TransformSpecSource::Tree(tree) = &cli.spec_source {
        // Capture the whole authored tree, including YAMLs outside the chunk
        // selected for this graph. Path membership is part of the analysis input.
        let members = graph_provenance::collect_yaml(&tree.modules_root)?;
        let paths = members
            .iter()
            .map(|member| tree.modules_root.join(member))
            .collect::<Vec<_>>();
        graph_provenance::record_yaml_membership(&tree.modules_root, &paths)?;
        for path in &paths {
            graph_provenance::read_bytes(path)?;
        }
    }
    let spec = load_transform_spec_source(&cli.spec_source)?;
    if let Some(expected) = expected_spec {
        anyhow::ensure!(
            serde_json::to_vec(&spec)? == serde_json::to_vec(expected)?,
            "authored spec changed during contract audit"
        );
    }
    if let TransformSpecSource::Tree(tree) = &cli.spec_source {
        capture.set_tree_scope(&tree.config_path, graph_module_roots(&tree.config_path)?)?;
    }
    let source_root = match &cli.spec_source {
        TransformSpecSource::Tree(tree) => tree.source_root.clone(),
        TransformSpecSource::Flat { .. } => None,
    }
    .unwrap_or_else(|| std::env::current_dir().expect("current directory"));
    capture.set_source_root(&source_root)?;
    validate_transform_spec(&spec)?;
    if !options.dry_run {
        preflight_output_roots(&spec)?;
    }
    let artifact = load_js_chunks(&spec.inputs.input_root, &spec.inputs.js_list_path)?;
    let source = prepare_js_chunks(&spec, artifact)?;
    let source_program = &source.program;

    // Single post-prepare vendor resolution pass: validates every mark,
    // resolves boundary mappings, swap targets, and partial-swap symbol
    // tables once, and runs the plan-time consumer gate and the
    // full-swap caller import-alignment check before anything is written. Vendor
    // application below consumes this plan instead of re-resolving the
    // spec mid-pipeline.
    let swap_cfg = &spec.swap_vendor_chunks;
    let vendor_plan = build_vendor_resolution_plan(
        source_program.artifact(),
        source_program.indexes(),
        &spec.vendor,
        &VendorPlanOptions {
            package_roots: &cli.package_roots,
            packages_root: &cli.packages_root,
            output_manifest_path: swap_cfg.output_manifest_path.as_deref(),
            output_wrapper_dir: swap_cfg.output_wrapper_dir.as_deref(),
        },
    )?;
    let graph_provenance = capture.finish()?;
    Ok((
        spec,
        PreparedTransform {
            source,
            vendor_plan: Arc::new(vendor_plan),
            provenance: graph_provenance,
        },
    ))
}

fn run_prepared_transform(
    spec: &TransformSpec,
    prepared: PreparedTransform,
    options: TransformRunOptions,
    suppress_reports: bool,
) -> Result<()> {
    let PreparedTransform {
        source,
        vendor_plan,
        provenance,
    } = prepared;
    let SourcePreparation {
        program: source_program,
        counts,
        chunk_records,
    } = source;
    let materialise_chunk_ids: Vec<String> = spec.materialize_chunk_ids().into_iter().collect();
    let swap_cfg = &spec.swap_vendor_chunks;
    let mut vendor_report = VendorSwapsReport::default();
    let write_vendor_outputs = swap_cfg.write && !options.dry_run;

    // Full swaps are an emission-set exclusion: the swapped chunks stay in
    // the bundle — nothing removes them, and the dependency graph never
    // contained vendor chunks — but emission, the rename queue, the
    // emit-shape check, and the chunk reports skip them. Their
    // wire-facing resolutions are projections of the plan.
    vendor_report.full = vendor_plan.full_swap_resolutions();

    let processed_chunk_names: BTreeSet<String> = materialise_chunk_ids.iter().cloned().collect();
    let mut module_count: usize = 0;
    let mut selected_lowerings: Vec<artifact::reports::SelectedModuleLowering> = Vec::new();
    let mut decomposition_by_chunk: HashMap<ChunkId, ChunkDecompositionOutput> = HashMap::new();
    // Spec member claims whose `binding.name` did not resolve to a
    // top-level declaration in the source chunk. Collected across
    // every chunk by `materialize_logical_modules`; surfaced at the
    // final validation boundary so the user sees the full list without
    // mistaking partial reconstruction for accepted output.
    let mut unmatched_spec_claims: Vec<UnmatchedSpecClaim> = Vec::new();
    // Vendor rewrites lowering applied at construction time inside
    // materialized module bodies, keyed by (swapped chunk, chunk
    // export); folded into the partial-swap manifests below so
    // `references_rewritten` keeps counting emitted references across
    // both application sites.
    let mut vendor_lowering_rewrites: BTreeMap<(ChunkId, String), usize> = BTreeMap::new();
    let mut emission_files = if !materialise_chunk_ids.is_empty() {
        let report_out_dir = spec.materialize_logical_modules.report_out_dir.clone();
        let report_out_dir = options.report_dir_override.clone().or(report_out_dir);
        let materialize_result = materialize_logical_modules(
            source_program.into_artifact(),
            MaterializeSpecInputs {
                execution_runtime: spec.execution_runtime,
                logical_modules: &spec.logical_modules,
                chunk_renames: &spec.chunk_renames,
                unassigned_mode: &spec.unassigned_mode,
                chunk_analysis_options: &spec.chunk_analysis_options,
                chunk_export_purity: &spec.chunk_export_purity,
                host_no_reentry: spec.host_no_reentry.as_ref(),
            },
            &vendor_plan,
            MaterializeLogicalModulesOptions {
                graph_provenance: Some(provenance),
                config: spec.materialize_logical_modules.clone(),
                chunk_ids: materialise_chunk_ids,
                keep_going: options.keep_going,
                list_template_identifiers: options.list_template_identifiers,
                // Dry-run keeps the no-output contract on the
                // accept path but still materializes rejection
                // evidence (dependency graph + cycles/conflicts) at the
                // same reports/tree/<chunk>/ location a real run
                // uses, so `debundle gate list/describe` works on
                // the rejection that was just reported.
                report_emission: match report_out_dir.filter(|_| !suppress_reports) {
                    Some(dir) if options.dry_run => ReportEmission::OnRejection(dir),
                    Some(dir) => ReportEmission::Full(dir),
                    None => ReportEmission::None,
                },
            },
        )?;
        module_count = materialize_result.module_count;
        selected_lowerings = materialize_result.selected_lowerings;
        unmatched_spec_claims = materialize_result.unmatched_spec_claims;
        vendor_lowering_rewrites = materialize_result.vendor_reference_rewrites;
        let outputs = materialize_result.output;
        decomposition_by_chunk = outputs.decomposition_by_chunk;
        outputs.files
    } else {
        artifact::EmissionFiles::from_prepared(source_program)
    };

    // Cross-chunk import naturalization runs first, before the following
    // emission rewrites canonicalize source specifiers. The two operations
    // change AST bodies without changing the indexed output file set.
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
    let emission_references;
    (emission_files, emission_references) =
        emission_files.rewrite_bodies(|artifact, indexes| {
            naturalize_cross_chunk_imports(
                artifact,
                indexes,
                &selected_lowerings,
                &processed_chunk_names,
            )?;
            apply_emission_rewrites_in_place(artifact, &vendor_plan, indexes)
        })?;
    merge_rewrite_counts(&mut vendor_rewrite_counts, emission_references);

    (vendor_report.partial, vendor_report.bundled_partial) =
        build_partial_swap_resolutions(&vendor_plan, &vendor_rewrite_counts)?;

    // Trim dead named import specifiers across the whole bundle (entry
    // included): the lowerer over-imports residual/hub bindings the
    // importing file never references. Runs immediately before the
    // export prune so that dropping a dead import — which the
    // name-based export prune would otherwise treat as a live consumer —
    // cascades into dropping the now-unimported module exports. See
    // prune_dead_imports.rs for the soundness argument.
    // Drop dead named exports from emitted logical-module files: a
    // module-owned binding referenced nowhere outside its own module
    // (the esbuild decorator scaffolding is the dominant case) is
    // module-internal, so it should not appear in the module's
    // `export { ... }`. Runs over the final bundle so it sees every
    // import/re-export; entry files (the chunk public surface) are
    // never touched. See prune_module_exports.rs for the soundness
    // argument.
    (emission_files, ()) = emission_files.rewrite_bodies(|files, _| {
        prune_dead_import_specifiers(files);
        prune_unimported_module_exports(files);
        Ok(())
    })?;

    // Freeze the exact final syntax and replacement decisions before any
    // accepted-program output is written. Diagnostic reports may already exist.
    let validated_plan = ReconstructionPlan::new(
        emission_files,
        vendor_plan,
        unmatched_spec_claims,
        spec.emit_browser_harness.as_ref(),
        VendorManifestOutput::prepare(swap_cfg.output_manifest_path.as_deref(), vendor_report),
    )?
    .validate()?;
    let emission_files = validated_plan.files();
    let excluded_chunk_ids = validated_plan.excluded_chunk_ids();

    // Vendor emission outputs: full-swap wrappers and bundled bundle
    // copies / facades, plus the combined manifest — all write-gated
    // behind `swap_vendor_chunks.write`.
    if write_vendor_outputs {
        validated_plan.write_vendor_outputs()?;
    }

    // Chunk records of the emission set: records of excluded
    // (fully-swapped) chunks are dropped from the emitted reports.
    let excluded_chunk_names: BTreeSet<&str> = excluded_chunk_ids
        .iter()
        .map(|chunk_id| emission_files.files().chunk_table.name(*chunk_id))
        .collect();
    let emitted_chunk_records: Vec<_> = chunk_records
        .into_iter()
        .filter(|record| !excluded_chunk_names.contains(record.chunk_id.as_str()))
        .collect();

    if !options.dry_run
        && let Some(cfg) = &spec.write_js_tree
    {
        write_js_tree(&WriteTreeInput {
            plan: &validated_plan,
            out_dir: &cfg.out_dir,
            lowerings: &selected_lowerings,
            counts: &counts,
            chunk_records: &emitted_chunk_records,
            module_count,
            decomposition_by_chunk: &decomposition_by_chunk,
        })?;
    }

    if !options.dry_run && validated_plan.browser_harness().is_some() {
        emit_browser_harness(
            &validated_plan,
            &emitted_chunk_records,
            &decomposition_by_chunk,
        )?;
    }

    Ok(())
}

fn provenance_roots(cli: &TransformCli) -> Result<(ProvenanceRoots, Option<PathBuf>)> {
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
    let mut packages = std::collections::BTreeMap::new();
    anyhow::ensure!(
        !cli.package_roots.contains_key("__packages_root__"),
        "package name __packages_root__ is reserved for the shared packages root in graph provenance"
    );
    for (name, root) in &cli.package_roots {
        packages.insert(name.clone(), graph_provenance::absolute(root)?);
    }
    if let Some(root) = &cli.packages_root {
        packages.insert(
            "__packages_root__".to_string(),
            graph_provenance::absolute(root)?,
        );
    }
    Ok((
        ProvenanceRoots {
            spec: spec_root,
            source,
            packages,
        },
        modules_directory,
    ))
}

/// A runnable argv containing the effective inputs, including values supplied
/// only through Clap env vars. All input and output roots are absolute so a
/// caller can replay it from another working directory (or translate roots
/// for an isolated snapshot).
fn canonical_generation_command(
    cli: &TransformCli,
    options: &TransformRunOptions,
    roots: &ProvenanceRoots,
) -> Result<Vec<String>> {
    fn path_arg(path: &Path) -> Result<String> {
        Ok(graph_provenance::absolute(path)?
            .to_str()
            .with_context(|| format!("non-UTF-8 generation path {}", path.display()))?
            .to_string())
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
            argv.extend(["--tree-source-root".to_string(), path_arg(&roots.source)?]);
            argv.extend(["--out-root".to_string(), path_arg(&tree.out_root)?]);
        }
    }
    let mut package_roots = cli.package_roots.iter().collect::<Vec<_>>();
    package_roots.sort_by_key(|(name, _)| *name);
    for (name, path) in package_roots {
        argv.extend([
            "--package-root".to_string(),
            format!("{name}={}", path_arg(path)?),
        ]);
    }
    if let Some(root) = &cli.packages_root {
        argv.extend(["--packages-root".to_string(), path_arg(root)?]);
    }
    if options.dry_run {
        argv.push("--dry-run".to_string());
    }
    if !options.keep_going {
        argv.push("--fail-fast".to_string());
    }
    Ok(argv)
}

pub fn load_transform_spec_source(source: &TransformSpecSource) -> Result<TransformSpec> {
    match source {
        TransformSpecSource::Flat { path } => read_flat_transform_spec(path),
        TransformSpecSource::Tree(options) => compile_spec_tree(options),
    }
}

fn validate_transform_spec(spec: &TransformSpec) -> Result<()> {
    if let Some(spec::HostNoReentry {
        runtime:
            spec::HostNoReentryRuntime::BunStandaloneBuild {
                version,
                revision,
                executable_sha256,
            },
        ..
    }) = &spec.host_no_reentry
    {
        anyhow::ensure!(
            !version.is_empty() && !version.chars().any(char::is_whitespace),
            "host_no_reentry.runtime.bun_standalone_build.version must name the audited Bun version"
        );
        for (field, digest, length) in [
            ("revision", revision, 40),
            ("executable_sha256", executable_sha256, 64),
        ] {
            anyhow::ensure!(
                digest.len() == length && digest.bytes().all(|byte| byte.is_ascii_hexdigit()),
                "host_no_reentry.runtime.bun_standalone_build.{field} must contain {length} hexadecimal characters"
            );
        }
    }
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
    use serde::Serialize;
    use spec::SwapVendorChunksConfig;
    use std::collections::BTreeMap;

    const KEEP_GOING_RUN: TransformRunOptions = TransformRunOptions {
        dry_run: false,
        keep_going: true,
        report_dir_override: None,
        list_template_identifiers: false,
    };

    // This real-pipeline boundary test changes files between preparation and
    // probing, which a black-box CLI test cannot synchronize without timing.
    #[test]
    fn prepared_contract_probes_abort_when_captured_inputs_change() -> Result<()> {
        js_ast::with_swc_globals(|| {
            #[derive(Serialize)]
            struct AuditSpec<'a> {
                inputs: PipelineSpecInputs<'a>,
            }
            let temp = tempfile::tempdir()?;
            let root = temp.path();
            let source = root.join("main.js");
            let list = root.join("js-files.txt");
            let spec_path = root.join("spec.yaml");
            fs::write(&source, "export const answer = 42;\n")?;
            fs::write(&list, "main.js\n")?;
            fs::write(
                &spec_path,
                serde_yaml::to_string(&AuditSpec {
                    inputs: PipelineSpecInputs {
                        input_root: root,
                        js_list_path: &list,
                    },
                })?,
            )?;
            let cli = TransformCli {
                spec_source: TransformSpecSource::Flat {
                    path: spec_path.clone(),
                },
                package_roots: HashMap::new(),
                packages_root: None,
            };
            let spec = load_transform_spec_source(&cli.spec_source)?;
            let audit = ContractAudit::prepare(&cli, &spec)?;
            for path in [&source, &list, &spec_path] {
                let original = fs::read(path)?;
                let mut changed = original.clone();
                changed.push(b'\n');
                fs::write(path, changed)?;
                let error = audit.probe(BTreeMap::new()).unwrap_err();
                assert!(error.is::<graph_provenance::GraphMismatch>(), "{error:#}");
                fs::write(path, original)?;
                assert!(matches!(
                    audit.probe(BTreeMap::new())?,
                    ContractProbeOutcome::Accepted
                ));
            }
            Ok(())
        })
    }

    #[test]
    fn reserved_package_root_name_is_rejected() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let mut package_roots = HashMap::new();
        package_roots.insert("__packages_root__".to_string(), temp.path().to_path_buf());
        let cli = TransformCli {
            spec_source: TransformSpecSource::Flat {
                path: temp.path().join("spec.yaml"),
            },
            package_roots,
            packages_root: Some(temp.path().to_path_buf()),
        };
        assert!(
            provenance_roots(&cli)
                .unwrap_err()
                .to_string()
                .contains("reserved")
        );
        Ok(())
    }

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

            run_transform_cli(
                &TransformCli {
                    spec_source: TransformSpecSource::Flat { path: spec_path },
                    package_roots: HashMap::new(),
                    packages_root: None,
                },
                KEEP_GOING_RUN,
            )?;

            assert!(out.join("app/bootstrap.js").exists());
            assert!(out.join("reports/runtime.json").exists());
            let entry = fs::read_to_string(out.join("app/static/index-EXAMPLE/entry.js"))?;
            assert!(entry.contains("../chunk-DuckMock/entry.js"));

            let output: serde_json::Value =
                serde_json::from_str(&fs::read_to_string(out.join("reports/output.json"))?)?;
            assert_eq!(
                output
                    .pointer("/output_metrics/total/files")
                    .and_then(serde_json::Value::as_u64),
                Some(2),
            );
            let runtime: serde_json::Value =
                serde_json::from_str(&fs::read_to_string(out.join("reports/runtime.json"))?)?;
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

            let err = run_transform_cli(
                &TransformCli {
                    spec_source: TransformSpecSource::Flat { path: spec_path },
                    package_roots: HashMap::new(),
                    packages_root: None,
                },
                KEEP_GOING_RUN,
            )
            .expect_err("non-empty output directories should be rejected, not replaced");
            assert!(js_out.join("stale.txt").exists());
            assert!(harness_out.join("stale.txt").exists());
            // Refused before any write: a later failure would leave `app/` or `reports/` behind.
            for out_dir in [&js_out, &harness_out] {
                assert!(!out_dir.join("app").exists(), "{err:#}");
                assert!(!out_dir.join("reports").exists(), "{err:#}");
            }
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

            run_transform_cli(
                &TransformCli {
                    spec_source: TransformSpecSource::Flat { path: spec_path },
                    package_roots: HashMap::new(),
                    packages_root: None,
                },
                TransformRunOptions {
                    dry_run: true,
                    keep_going: false,
                    report_dir_override: None,
                    list_template_identifiers: false,
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
            execution_runtime: spec::ExecutionRuntime::default(),
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
            host_no_reentry: None,
            swap_vendor_chunks: SwapVendorChunksConfig::default(),
            materialize_logical_modules: spec::MaterializeLogicalModulesConfig::default(),
            write_js_tree: None,
            emit_browser_harness: None,
        }
    }
}
