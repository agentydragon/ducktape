#[path = "pipeline/apply_projection.rs"]
mod apply_projection;
#[path = "apply_projection_codec.rs"]
mod apply_projection_codec;
#[path = "pipeline/preflight.rs"]
mod preflight;
#[path = "pipeline/probes.rs"]
mod probes;
#[path = "pipeline/provenance.rs"]
mod provenance;
#[path = "pipeline/selected_ownership.rs"]
mod selected_ownership;

pub use apply_projection::project_apply;
use apply_projection::{
    APPLY_OUTPUT_ROOT, ApplyPath, ApplyProjection, map_apply_spec_paths, map_tree_output_paths,
    project_apply_from_spec, projection_source,
};
use preflight::{preflight_output_roots, preflight_roots, validate_transform_spec};
pub use probes::{AnnotationProbe, AnnotationProbeRun, ContractAudit, ContractProbeOutcome};
use provenance::{canonical_generation_command, provenance_roots};
pub use selected_ownership::{SelectedOwnershipProgram, SelectedOwnershipProgramKey};
use transform_inputs::{TransformRequest, TransformSpecSource, load_transform_spec_with_roots};

use artifact::load_js_chunks;
use source_identity::ChunkId;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use anyhow::{Context, Result, bail};

use analysis_projection::AnalysisSpec;
use artifact::reports::{ArtifactChunkRecord, ArtifactCounts, ChunkDecompositionOutput};
use emit_harness::emit_browser_harness;
use final_global_resolution::GlobalResolutionSources;
use gate::{CandidateRelocationAssessmentReport, UnmatchedSpecClaim};
use graph_provenance::{
    CurrentRoots, GraphProvenance, InputCapture, ProvenanceRoots, VerifiedGraphSourceContext,
};
use lowering::{
    AnalysisMode, CandidateEmissionDraft, FullReportKind, GateRejectionPolicy,
    GraphReportSelection, MaterializeLogicalModulesOptions, MaterializeSpecInputs, Materialized,
    ReportEmission, materialize_logical_modules,
};
use output_layout::GLOBAL_RESOLUTION_REPORT;
use ownership_subset::OwnershipSubset;
use prepare_chunks::prepare_js_chunks;
use reconstruction::{EmittablePlan, RejectedPlan, VendorManifestOutput, validate_reconstruction};
use spec::{ChunkExportPurity, LogicalModule, NodeBuiltinModuleExt, TransformSpec};
use spec_tree::graph_module_roots;
use vendor::{VendorPlanOptions, VendorResolutionPlan, build_vendor_resolution_plan};
use write_tree::{RejectedArtifacts, WriteTreeInput, write_gate_rejected_marker, write_js_tree};

/// Exit status of `debundle run --emit-on-rejection` when the realizability
/// gate's rejection is the run's only failure and the marked reading-only tree
/// is complete. Sync: `_GATE_REJECTED_EXIT_CODE` in `pipeline.bzl`.
pub const GATE_REJECTED_EXIT_CODE: u8 = 3;

/// The failure [`GATE_REJECTED_EXIT_CODE`] reports; displays the gate's
/// diagnostic.
#[derive(Debug)]
pub struct GateRejected(String);

impl std::fmt::Display for GateRejected {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for GateRejected {}

/// What a run writes once the pipeline checks have run. The checks themselves
/// (selectors, realizability gate, final program validation) are the same in
/// every mode, as is the nonzero exit on rejection.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RunOutput {
    /// Nothing on the accept path; a gate rejection still writes its
    /// evidence reports.
    DryRun,
    /// The accepted tree, browser harness and vendor assets; a rejection
    /// writes diagnostics only.
    Accepted,
    /// [`Self::Accepted`], and on a realizability-gate rejection the
    /// logical-module tree anyway, marked unrunnable (`GATE_REJECTED` marker,
    /// entry banner) and without browser harness or vendor assets.
    EmitOnRejection,
}

/// Output artifacts after the same canonical planning and final validation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RunArtifacts {
    Program,
    ReportsOnly { out_root: PathBuf },
}

impl RunArtifacts {
    fn writes_program(&self) -> bool {
        matches!(self, Self::Program)
    }

    fn reports_root(&self) -> Option<PathBuf> {
        match self {
            Self::Program => None,
            Self::ReportsOnly { out_root } => Some(out_root.join("reports/tree")),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TransformRunOptions {
    /// Retain only these ownership claims after resolving the complete spec.
    pub ownership_subset: Option<OwnershipSubset>,
    /// Original CLI file and exact bytes parsed into `ownership_subset`.
    /// Library callers may supply a normalized subset without a file input.
    pub ownership_subset_input: Option<OwnershipSubsetInput>,
    pub output: RunOutput,
    pub artifacts: RunArtifacts,
    pub graph_reports: GraphReportSelection,
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
    /// Stream complete effect-conflict JSONL beside each emitted summary.
    pub effect_conflicts_jsonl: bool,
    /// Emission-only JSONL selection; empty exports all materialized chunks.
    pub effect_conflicts_chunks: BTreeSet<String>,
    pub full_reports: BTreeSet<FullReportKind>,
    pub full_report_chunks: BTreeSet<String>,
}

impl TransformRunOptions {
    /// A dry run with no reports: nothing is emitted, no per-chunk report is
    /// written, no ownership subset narrows the run. Contract and annotation
    /// probes run exactly this shape; other callers override the fields their
    /// command decides.
    pub fn dry_run() -> Self {
        Self {
            ownership_subset: None,
            ownership_subset_input: None,
            output: RunOutput::DryRun,
            artifacts: RunArtifacts::Program,
            graph_reports: GraphReportSelection::None,
            keep_going: false,
            report_dir_override: None,
            list_template_identifiers: false,
            effect_conflicts_jsonl: false,
            effect_conflicts_chunks: BTreeSet::new(),
            full_reports: BTreeSet::new(),
            full_report_chunks: BTreeSet::new(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OwnershipSubsetInput {
    pub path: PathBuf,
    pub bytes: Vec<u8>,
}

pub fn run_transform(
    cli: &TransformRequest,
    options: TransformRunOptions,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<()> {
    run_transform_with_analysis(cli, options, None, diagnostics)
}

/// A stored artifact paired with the reader's compiled analysis identity.
#[derive(Clone, Copy)]
pub struct AnalysisArtifactInput<'a> {
    pub path: &'a Path,
    pub analysis_engine_sha256: &'a str,
}

/// `debundle run`, taking analysis from the artifact at `analysis` when given
/// (`debundle run --analysis`).
pub fn run_transform_with_analysis(
    cli: &TransformRequest,
    options: TransformRunOptions,
    analysis: Option<AnalysisArtifactInput<'_>>,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<()> {
    run_transform_with_projection(
        cli,
        options,
        analysis,
        None,
        &std::env::current_dir()?,
        diagnostics,
    )
}

/// Apply a canonical spec without reopening authored YAML. Full graph export
/// requires the authored input capture and is deliberately unsupported here.
pub fn run_transform_with_projection(
    cli: &TransformRequest,
    options: TransformRunOptions,
    analysis: Option<AnalysisArtifactInput<'_>>,
    projection_path: Option<&Path>,
    projection_root: &Path,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<()> {
    let projection = projection_path
        .map(|path| -> Result<ApplyProjection> {
            anyhow::ensure!(analysis.is_some(), "--apply-projection requires --analysis");
            anyhow::ensure!(
                !options.graph_reports.enabled(),
                "--apply-projection cannot export replayable graphs; use authored spec inputs"
            );
            let mut projection = ApplyProjection::decode(&fs::read(path)?)?;
            let root = std::path::absolute(projection_root)?;
            anyhow::ensure!(
                projection.source == projection_source(&cli.spec_source, &root)?,
                "apply projection was compiled for other spec paths"
            );
            map_apply_spec_paths(&mut projection.spec, &mut |field, path| {
                Ok(if projection.rooted_paths.contains(&field) {
                    root.join(path)
                } else {
                    path.to_path_buf()
                })
            })?;
            if let TransformSpecSource::Tree(tree) = &cli.spec_source {
                map_tree_output_paths(
                    &mut projection.spec,
                    Path::new(APPLY_OUTPUT_ROOT),
                    &tree.out_root,
                )?;
            }
            Ok(projection)
        })
        .transpose()?;
    if let RunArtifacts::ReportsOnly { out_root } = &options.artifacts {
        anyhow::ensure!(analysis.is_some(), "--reports-only requires --analysis");
        anyhow::ensure!(
            options.output != RunOutput::DryRun,
            "--reports-only conflicts with --dry-run"
        );
        anyhow::ensure!(
            options.report_dir_override.is_none(),
            "--reports-only conflicts with a report directory override"
        );
        anyhow::ensure!(
            !out_root.as_os_str().is_empty(),
            "--reports-only requires a nonempty output root"
        );
        if let TransformSpecSource::Tree(tree) = &cli.spec_source {
            anyhow::ensure!(
                graph_provenance::absolute(out_root)?
                    == graph_provenance::absolute(&tree.out_root)?,
                "--reports-only and --out-root must name the same output root"
            );
        }
    }
    let (spec, prepared) = prepare_transform(
        cli,
        &options,
        None,
        analysis.map(|input| input.path),
        projection.as_ref(),
    )?;
    let mode = match analysis {
        Some(AnalysisArtifactInput {
            path,
            analysis_engine_sha256,
        }) => AnalysisMode::Load {
            base: Box::new(AnalysisSpec::from_transform(
                &spec,
                &prepared.provenance.roots.source,
            )?),
            artifact: fs::read(path)
                .with_context(|| format!("read analysis artifact {}", path.display()))?,
            analysis_engine_sha256: analysis_engine_sha256.to_string(),
        },
        None => AnalysisMode::Compute,
    };
    run_prepared_transform(&spec, prepared, options, false, mode, false, diagnostics)?;
    Ok(())
}

/// Build a report-only assessment from the exact candidate module documents
/// used by a graph-backed `bindings assign` dry run. The tree source, staged
/// analysis and graph must all identify the same chunk input.
pub fn assess_candidate_effect_relocation(
    cli: &TransformRequest,
    analysis: AnalysisArtifactInput<'_>,
    graph_source: &VerifiedGraphSourceContext,
    edit_modules_root: &Path,
    candidate_modules: &BTreeMap<String, LogicalModule>,
    import_order: &[spec::ModulePath],
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<CandidateRelocationAssessmentReport> {
    let TransformSpecSource::Tree(tree) = &cli.spec_source else {
        bail!("candidate effect assessment requires a tree-shaped transform context");
    };
    let tree_modules_root = graph_provenance::absolute(&tree.modules_root)?;
    let edit_modules_root = graph_provenance::absolute(edit_modules_root)?;
    anyhow::ensure!(
        tree_modules_root == edit_modules_root,
        "candidate assessment requires --tree-modules to equal --modules"
    );
    anyhow::ensure!(
        graph_source
            .modules_root()
            .map(graph_provenance::absolute)
            .transpose()?
            .as_ref()
            == Some(&tree_modules_root),
        "candidate transform context modules root differs from the verified graph"
    );
    let scoped_roots = graph_module_roots(&tree.config_path)?;
    let roots = scoped_roots.get(graph_source.chunk_id()).with_context(|| {
        format!(
            "tree config has no module root for graph chunk {:?}",
            graph_source.chunk_id()
        )
    })?;
    anyhow::ensure!(
        roots.len() == 1 && roots[0].as_os_str().is_empty(),
        "candidate effect assessment currently requires one full-tree module root for graph chunk {:?}",
        graph_source.chunk_id()
    );

    let options = TransformRunOptions::dry_run();
    let (mut spec, prepared) = prepare_transform(cli, &options, None, Some(analysis.path), None)?;
    let chunk_source = prepared
        .provenance
        .chunk_sources
        .iter()
        .find(|source| source.chunk_id == graph_source.chunk_id())
        .with_context(|| {
            format!(
                "transform context has no input mapping for graph chunk {:?}",
                graph_source.chunk_id()
            )
        })?;
    let transform_input = prepared.provenance.resolve(
        &chunk_source.input_file.root,
        &chunk_source.input_file.path,
        &CurrentRoots::default(),
    )?;
    anyhow::ensure!(
        chunk_source.source_path == graph_source.source_path()
            && chunk_source.sha256 == graph_source.sha256()
            && graph_provenance::absolute(&transform_input)? == graph_source.input_file(),
        "candidate transform context source differs from the verified dependency graph"
    );
    let materialized_chunks = spec.materialize_chunk_ids();
    anyhow::ensure!(
        materialized_chunks.len() == 1 && materialized_chunks.contains(graph_source.chunk_id()),
        "candidate effect assessment currently requires exactly the graph chunk {:?} to be materialized",
        graph_source.chunk_id(),
    );
    spec.logical_modules.insert(
        graph_source.chunk_id().to_string(),
        candidate_modules.clone(),
    );
    spec.import_order
        .insert(graph_source.chunk_id().to_string(), import_order.to_vec());
    validate_transform_spec(&spec)?;

    let artifact = fs::read(analysis.path)
        .with_context(|| format!("read analysis artifact {}", analysis.path.display()))?;
    let mode = AnalysisMode::Load {
        base: Box::new(AnalysisSpec::from_transform(
            &spec,
            &prepared.provenance.roots.source,
        )?),
        artifact,
        analysis_engine_sha256: analysis.analysis_engine_sha256.to_string(),
    };
    let outcome = run_prepared_transform(&spec, prepared, options, true, mode, true, diagnostics)?;
    let PreparedTransformResult::EffectAssessed(mut assessments) = outcome else {
        bail!("candidate effect assessment did not reach AssignmentAnalysis");
    };
    let mut matches = assessments
        .drain(..)
        .filter(|(chunk_id, _)| chunk_id == graph_source.chunk_id());
    let (_, assessment) = matches.next().with_context(|| {
        format!(
            "no effect assessment for chunk {:?}",
            graph_source.chunk_id()
        )
    })?;
    anyhow::ensure!(
        matches.next().is_none(),
        "candidate transform produced duplicate effect assessments for chunk {:?}",
        graph_source.chunk_id()
    );
    Ok(assessment)
}

/// An edit that supplied verified staged facts may write only when the
/// canonical placement projection yielded the maps normal emission uses.
/// Effect conflicts remain report-only while the legacy gate is authoritative.
pub fn ensure_candidate_emittable_for_write(
    assessment: &CandidateRelocationAssessmentReport,
    dry_run: bool,
) -> Result<()> {
    if !dry_run {
        anyhow::ensure!(
            assessment.placement.emission_mappable,
            "candidate placement is unmappable for canonical emission: {:?}; unresolved checks: {:?}",
            assessment.placement.projection_violations,
            assessment.placement.coverage.unresolved_checks,
        );
    }
    Ok(())
}

/// `debundle spec project-analysis`: the run's analysis projection, written to
/// `out` as its canonical bytes. Runs the selector solve and nothing after it.
pub fn project_analysis(
    cli: &TransformRequest,
    out: &Path,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<()> {
    project_analysis_and_apply(cli, out, None, diagnostics)
}

/// Project both stage inputs from one loaded spec. The apply artifact is
/// optional so the standalone analysis projector retains its public behavior.
pub fn project_analysis_and_apply(
    cli: &TransformRequest,
    analysis_out: &Path,
    apply_out: Option<(&Path, &Path)>,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<()> {
    let options = TransformRunOptions::dry_run();
    let (spec, prepared) = prepare_transform(cli, &options, None, None, None)?;
    if let Some((root, out)) = apply_out {
        project_apply_from_spec(
            cli,
            root,
            out,
            spec.clone(),
            prepared.provenance.module_roots_by_chunk.clone(),
        )?;
    }
    let base = AnalysisSpec::from_transform(&spec, &prepared.provenance.roots.source)?;
    let artifact = prepared.source.into_artifact();
    let projection = frontend_elaboration::project_analysis(
        artifact,
        &spec,
        &base,
        Some(&prepared.provenance),
        diagnostics,
    )?;
    fs::write(analysis_out, projection.encode()?)
        .with_context(|| format!("write analysis projection {}", analysis_out.display()))
}

/// Input-only preparation shared by the baseline and independent contract probes.
/// Each run gets its own mutable AST; vendor decisions remain read-only.
#[derive(Clone)]
struct PreparedTransform {
    source: artifact::SourceProgram,
    vendor_plan: Arc<VendorResolutionPlan>,
    provenance: GraphProvenance,
    /// Host loader per embedded input file.
    embedded_loaders: BTreeMap<String, spec::BunLoader>,
}

/// Sink selection never changes the analyzed or materialized source scope.
fn validate_report_chunk_selection(
    selector: &str,
    export: &str,
    enabled: bool,
    selected: &BTreeSet<String>,
    materialized: &[String],
) -> Result<()> {
    if selected.is_empty() {
        return Ok(());
    }
    if !enabled {
        bail!("{selector} requires {export}");
    }
    let materialized: BTreeSet<_> = materialized
        .iter()
        .map(|chunk| artifact::paths::normalize_module_path(chunk))
        .collect::<Result<_>>()?;
    for chunk in selected {
        if !materialized.contains(chunk) {
            bail!("unknown {selector} {chunk:?}: expected a configured materialized chunk");
        }
    }
    Ok(())
}

fn prepare_transform(
    cli: &TransformRequest,
    options: &TransformRunOptions,
    expected_spec: Option<&TransformSpec>,
    loaded_analysis: Option<&Path>,
    projection: Option<&ApplyProjection>,
) -> Result<(TransformSpec, PreparedTransform)> {
    let (roots, modules_directory) = provenance_roots(cli)?;
    let generation_command = canonical_generation_command(cli, options, &roots, loaded_analysis)?;
    let capture = InputCapture::start(roots, modules_directory, generation_command)?;
    if let Some(input) = &options.ownership_subset_input {
        anyhow::ensure!(
            options.ownership_subset.is_some(),
            "ownership subset input bytes were retained without a parsed subset"
        );
        let parsed: OwnershipSubset = serde_json::from_slice(&input.bytes)
            .context("parsing retained ownership subset input bytes")?;
        anyhow::ensure!(
            options.ownership_subset.as_ref() == Some(&parsed),
            "parsed ownership subset differs from the exact retained input bytes"
        );
        capture.record_ownership_subset(&input.path, &input.bytes)?;
    }
    anyhow::ensure!(
        options.ownership_subset.is_none()
            || options.ownership_subset_input.is_some()
            || !options.graph_reports.enabled(),
        "graph export for ownership subsets requires the exact --ownership-subset input file"
    );
    if let TransformSpecSource::Tree(tree) = &cli.spec_source
        && projection.is_none()
    {
        // Capture the whole authored tree, including YAMLs outside the chunk
        // selected for this graph. Path membership is part of the analysis input.
        for path in graph_provenance::yaml_files(&tree.modules_root)? {
            graph_provenance::read_bytes(&path)?;
        }
    }
    let (spec, module_roots_by_chunk) = match projection {
        Some(projection) => (
            projection.spec.clone(),
            projection.module_roots_by_chunk.clone(),
        ),
        None => load_transform_spec_with_roots(&cli.spec_source)?,
    };
    if let Some(expected) = expected_spec {
        anyhow::ensure!(
            serde_json::to_vec(&spec)? == serde_json::to_vec(expected)?,
            "authored spec changed during contract audit"
        );
    }
    if let Some(subset) = &options.ownership_subset {
        capture
            .set_selected_ownership(subset.normalized_by_chunk(
                &spec.materialize_chunk_ids().into_iter().collect::<Vec<_>>(),
            ))?;
    }
    if let TransformSpecSource::Tree(tree) = &cli.spec_source {
        capture.set_tree_scope(&tree.config_path, module_roots_by_chunk)?;
    }
    validate_transform_spec(&spec)?;
    if let RunArtifacts::ReportsOnly { out_root } = &options.artifacts {
        preflight_roots(vec![out_root.clone()])?;
    } else if options.output != RunOutput::DryRun {
        preflight_output_roots(&spec)?;
    }
    let artifact = load_js_chunks(&spec.inputs.input_root, &spec.inputs.js_list_path)?;
    let embedded_loaders: BTreeMap<String, spec::BunLoader> =
        match &spec.inputs.embedded_loaders_path {
            Some(path) => serde_json::from_str(&graph_provenance::read_source_to_string(path)?)
                .with_context(|| format!("parse embedded loaders {}", path.display()))?,
            None => BTreeMap::new(),
        };
    let source = prepare_js_chunks(
        &spec.materialize_chunk_ids(),
        &spec.source_premise_chunk_ids(),
        &spec.vendor,
        artifact,
    )?;

    // Single post-prepare vendor resolution pass: validates every mark,
    // resolves boundary mappings, swap targets, and partial-swap symbol
    // tables once, and runs the plan-time consumer gate and the
    // full-swap caller import-alignment check before anything is written. Vendor
    // application below consumes this plan instead of re-resolving the
    // spec mid-pipeline.
    let swap_cfg = &spec.swap_vendor_chunks;
    let vendor_plan = build_vendor_resolution_plan(
        source.artifact(),
        source.indexes(),
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
            embedded_loaders,
        },
    ))
}

enum PreparedTransformResult {
    Completed,
    EffectAssessed(Vec<(String, CandidateRelocationAssessmentReport)>),
}

fn run_prepared_transform(
    spec: &TransformSpec,
    prepared: PreparedTransform,
    options: TransformRunOptions,
    suppress_reports: bool,
    analysis: AnalysisMode,
    assessment_only: bool,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<PreparedTransformResult> {
    let PreparedTransform {
        source: source_program,
        vendor_plan,
        provenance,
        embedded_loaders,
        ..
    } = prepared;
    let (counts, emitted_chunk_records) = source_report_records(
        &source_program.artifact().chunks,
        &vendor_plan.full_swap_chunk_ids(),
    );
    let materialise_chunk_ids: Vec<String> = spec.materialize_chunk_ids().into_iter().collect();
    anyhow::ensure!(
        options.ownership_subset.is_none() || !materialise_chunk_ids.is_empty(),
        "ownership subsets require at least one materialized chunk"
    );
    let dry_run = options.output == RunOutput::DryRun;
    validate_report_chunk_selection(
        "--effect-conflicts-chunk",
        "--effect-conflicts-jsonl",
        options.effect_conflicts_jsonl,
        &options.effect_conflicts_chunks,
        &materialise_chunk_ids,
    )?;
    validate_report_chunk_selection(
        "--full-report-chunk",
        "--full-report",
        !options.full_reports.is_empty(),
        &options.full_report_chunks,
        &materialise_chunk_ids,
    )?;
    let write_program = !dry_run && options.artifacts.writes_program();
    if (options.effect_conflicts_jsonl
        || !options.full_reports.is_empty()
        || options.graph_reports.enabled())
        && (suppress_reports
            || options
                .artifacts
                .reports_root()
                .as_ref()
                .or(options.report_dir_override.as_ref())
                .or(spec.materialize_logical_modules.report_out_dir.as_ref())
                .is_none())
    {
        let requested = if options.effect_conflicts_jsonl {
            "--effect-conflicts-jsonl"
        } else if !options.full_reports.is_empty() {
            "--full-report"
        } else {
            "--graphs"
        };
        bail!("{requested} requires a configured report output directory");
    }
    let swap_cfg = &spec.swap_vendor_chunks;
    let write_vendor_outputs = swap_cfg.write && write_program;

    let mut decomposition_by_chunk: HashMap<ChunkId, ChunkDecompositionOutput> = HashMap::new();
    // Spec member claims whose `binding.name` did not resolve to a
    // top-level declaration in the source chunk. Collected across
    // every chunk by `materialize_logical_modules`; surfaced at the
    // final validation boundary so the user sees the full list without
    // mistaking partial reconstruction for accepted output.
    let mut unmatched_spec_claims: Vec<UnmatchedSpecClaim> = Vec::new();
    // Chunks the gate rejected but lowered anyway (`RunOutput::EmitOnRejection`).
    let mut gate_rejections = Vec::new();
    let report_out_dir = options
        .artifacts
        .reports_root()
        .or(options.report_dir_override.clone())
        .or(spec.materialize_logical_modules.report_out_dir.clone());
    // Captured before materialization consumes the prepared artifact.
    let global_resolution_sources = GlobalResolutionSources::capture(source_program.artifact());
    if let Some(dir) = gate::tv_audit_dir() {
        write_tv_audit_sources(dir, source_program.artifact())?;
    }
    let candidate_draft = if !materialise_chunk_ids.is_empty() {
        let materialize_result = materialize_logical_modules(
            source_program.into_artifact(),
            MaterializeSpecInputs {
                host_behavior: &spec.host_behavior,
                program_premises: &spec.program_premises,
                embedded_loaders: &embedded_loaders,
                logical_modules: &spec.logical_modules,
                chunk_renames: &spec.chunk_renames,
                unassigned_mode: &spec.unassigned_mode,
                import_order: &spec.import_order,
                chunk_analysis_options: &spec.chunk_analysis_options,
                chunk_export_purity: &spec.chunk_export_purity,
            },
            &vendor_plan,
            MaterializeLogicalModulesOptions {
                ownership_subset: options.ownership_subset.clone(),
                graph_provenance: Some(provenance),
                config: spec.materialize_logical_modules.clone(),
                chunk_ids: materialise_chunk_ids,
                keep_going: options.keep_going,
                list_template_identifiers: options.list_template_identifiers,
                effect_conflicts_jsonl: options.effect_conflicts_jsonl,
                effect_conflicts_chunks: options.effect_conflicts_chunks.clone(),
                full_reports: options.full_reports.clone(),
                full_report_chunks: options.full_report_chunks.clone(),
                // Dry-run keeps the no-output contract on the
                // accept path but still materializes rejection
                // evidence at the same reports/tree/<chunk>/ location.
                // Graph-backed follow-ups additionally require --graphs.
                graph_reports: options.graph_reports,
                report_emission: match report_out_dir.clone().filter(|_| !suppress_reports) {
                    Some(dir) if dry_run => ReportEmission::OnRejection(dir),
                    Some(dir) => ReportEmission::Full(dir),
                    None => ReportEmission::None,
                },
                gate_rejection: match options.output {
                    RunOutput::DryRun | RunOutput::Accepted => GateRejectionPolicy::Refuse,
                    RunOutput::EmitOnRejection => GateRejectionPolicy::LowerUnrunnable,
                },
                assessment_only,
                analysis,
            },
            diagnostics,
        )?;
        let materialize_result = match materialize_result {
            Materialized::Assessed(assessments) => {
                return Ok(PreparedTransformResult::EffectAssessed(assessments));
            }
            Materialized::Lowered(result) if !assessment_only => *result,
            Materialized::Lowered(_) => {
                bail!("assessment-only materialization unexpectedly lowered chunks");
            }
        };
        unmatched_spec_claims = materialize_result.unmatched_spec_claims;
        gate_rejections = materialize_result.gate_rejections;
        decomposition_by_chunk = materialize_result.decomposition_by_chunk;
        materialize_result.output
    } else {
        CandidateEmissionDraft::pass_through(source_program, Arc::clone(&vendor_plan))
    };

    // The draft runs naturalization, vendor emission rewrites, and both
    // prune passes in their fixed order; its finalized files are immutable.
    let (candidate, selected_lowerings) = candidate_draft.finalize()?;
    let vendor_report = vendor_plan.swaps_report(candidate.references_by_symbol())?;

    // A gate-rejected program never reaches final program validation (as
    // without the flag, where the gate already failed the run): freeze it for
    // the marked reading-only tree, write that tree, and fail the run with the
    // gate's own diagnostic.
    if !gate_rejections.is_empty() {
        let rejected_plan = RejectedPlan::new(candidate, gate_rejections, &unmatched_spec_claims)?;
        // Where an accepted run writes its modules: the tree, else the harness.
        let tree_dir = spec
            .write_js_tree
            .as_ref()
            .map(|cfg| &cfg.out_dir)
            .or(spec.emit_browser_harness.as_ref().map(|cfg| &cfg.out_dir));
        if let RunArtifacts::ReportsOnly { out_root } = &options.artifacts {
            write_gate_rejected_marker(out_root, &rejected_plan, RejectedArtifacts::ReportsOnly)?;
        } else if let Some(out_dir) = tree_dir {
            write_js_tree(&WriteTreeInput {
                plan: EmittablePlan::Rejected(&rejected_plan),
                out_dir,
                lowerings: &selected_lowerings,
                counts: &counts,
                chunk_records: &emitted_chunk_records,
                decomposition_by_chunk: &decomposition_by_chunk,
            })?;
        }
        return Err(GateRejected(rejected_plan.diagnostic()).into());
    }

    // Freeze the exact final syntax and replacement decisions before any
    // accepted-program output is written. Diagnostic reports may already exist.
    let validated_plan = validate_reconstruction(
        candidate,
        &global_resolution_sources,
        &unmatched_spec_claims,
        spec.emit_browser_harness.as_ref(),
        VendorManifestOutput::prepare(swap_cfg.output_manifest_path.as_deref(), vendor_report),
    )?;

    if !dry_run
        && !suppress_reports
        && let Some(report_dir) = &report_out_dir
    {
        fs::create_dir_all(report_dir)
            .with_context(|| format!("create report directory {}", report_dir.display()))?;
        report_io::write_json(
            report_dir.join(GLOBAL_RESOLUTION_REPORT),
            validated_plan.global_resolution(),
        )?;
    }

    // Vendor emission outputs: full-swap wrappers and bundled bundle
    // copies / facades, plus the combined manifest — all write-gated
    // behind `swap_vendor_chunks.write`.
    if write_vendor_outputs {
        validated_plan.write_vendor_outputs()?;
    }

    if write_program && let Some(cfg) = &spec.write_js_tree {
        write_js_tree(&WriteTreeInput {
            plan: EmittablePlan::Accepted(&validated_plan),
            out_dir: &cfg.out_dir,
            lowerings: &selected_lowerings,
            counts: &counts,
            chunk_records: &emitted_chunk_records,
            decomposition_by_chunk: &decomposition_by_chunk,
        })?;
    }

    if write_program && validated_plan.browser_harness().is_some() {
        emit_browser_harness(
            &validated_plan,
            &emitted_chunk_records,
            &decomposition_by_chunk,
        )?;
    }

    Ok(PreparedTransformResult::Completed)
}

/// Totals describe the source bundle; chunk records describe the emission set.
fn source_report_records(
    chunks: &[artifact::ChunkArtifact],
    excluded_chunk_ids: &BTreeSet<ChunkId>,
) -> (ArtifactCounts, Vec<ArtifactChunkRecord>) {
    let mut counts = ArtifactCounts {
        kept_top_level_declarations: 0,
        top_level_side_effects: 0,
        export_aliases: 0,
        unresolved_exports: 0,
    };
    let mut chunk_records = Vec::with_capacity(chunks.len());
    for chunk in chunks {
        let analysis = &chunk.analysis;
        counts.kept_top_level_declarations += analysis.counts.kept_top_level_declarations;
        counts.top_level_side_effects += analysis.counts.top_level_side_effects;
        counts.export_aliases += analysis.counts.export_aliases;
        counts.unresolved_exports += analysis.counts.unresolved_exports;
        if excluded_chunk_ids.contains(&chunk.chunk_id) {
            continue;
        }
        chunk_records.push(ArtifactChunkRecord {
            chunk_id: analysis.chunk_id.clone(),
            source_path: analysis.source_path.clone(),
        });
    }
    (counts, chunk_records)
}

/// The input sources the `DEBUNDLE_TV_AUDIT` records' source locations index,
/// keyed by source path (`gate::tv_audit_dir`).
fn write_tv_audit_sources(dir: &Path, source: &artifact::ChunkBundle) -> Result<()> {
    let sources: BTreeMap<&str, String> = source
        .chunks
        .iter()
        .flat_map(|chunk| &chunk.js.files)
        .filter_map(|file| {
            let text = file
                .source()
                .map(str::to_owned)
                .or_else(|| file.ast().map(|ast| ast.source_text()))?;
            Some((file.metadata.source_path.as_str(), text))
        })
        .collect();
    fs::create_dir_all(dir)?;
    let path = dir.join(format!("{}.sources.json", std::process::id()));
    fs::write(&path, serde_json::to_vec(&sources)?).with_context(|| format!("write {path:?}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Serialize;
    use spec::SwapVendorChunksConfig;
    use std::collections::BTreeMap;
    use transform_inputs::load_transform_spec_source;

    fn keep_going_run() -> TransformRunOptions {
        TransformRunOptions {
            output: RunOutput::Accepted,
            keep_going: true,
            ..TransformRunOptions::dry_run()
        }
    }

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
            let cli = TransformRequest {
                spec_source: TransformSpecSource::Flat {
                    path: spec_path.clone(),
                },
                package_roots: HashMap::new(),
                packages_root: None,
            };
            let spec = load_transform_spec_source(&cli.spec_source)?;
            let audit = ContractAudit::prepare(
                &cli,
                &spec,
                &analysis::diagnostics::Diagnostics::default(),
            )?;
            for path in [&source, &list, &spec_path] {
                let original = fs::read(path)?;
                let mut changed = original.clone();
                changed.push(b'\n');
                fs::write(path, changed)?;
                let error = audit
                    .probe(
                        BTreeMap::new(),
                        &analysis::diagnostics::Diagnostics::default(),
                    )
                    .unwrap_err();
                assert!(error.is::<graph_provenance::GraphMismatch>(), "{error:#}");
                fs::write(path, original)?;
                assert!(matches!(
                    audit.probe(
                        BTreeMap::new(),
                        &analysis::diagnostics::Diagnostics::default()
                    )?,
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
        let cli = TransformRequest {
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

    struct ExistingOutputsFixture {
        _temp: tempfile::TempDir,
        js_out: PathBuf,
        harness_out: PathBuf,
        request: TransformRequest,
    }

    fn existing_outputs_fixture() -> Result<ExistingOutputsFixture> {
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
            r#"{"entryPoints":{"html":"index.html"}}"#,
        )?;

        let mut spec = empty_transform_spec();
        spec.inputs = spec::LoadJsChunksArgs {
            input_root: snapshot.clone(),
            js_list_path,
            embedded_loaders_path: None,
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

        Ok(ExistingOutputsFixture {
            _temp: temp,
            js_out,
            harness_out,
            request: TransformRequest {
                spec_source: TransformSpecSource::Flat { path: spec_path },
                package_roots: HashMap::new(),
                packages_root: None,
            },
        })
    }

    #[test]
    fn run_transform_writes_spec_pipeline_outputs() -> Result<()> {
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
                r#"{"entryPoints":{"html":"index.html"}}"#,
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

            run_transform(
                &TransformRequest {
                    spec_source: TransformSpecSource::Flat { path: spec_path },
                    package_roots: HashMap::new(),
                    packages_root: None,
                },
                keep_going_run(),
                &analysis::diagnostics::Diagnostics::default(),
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
            let fixture = existing_outputs_fixture()?;
            let err = run_transform(
                &fixture.request,
                keep_going_run(),
                &analysis::diagnostics::Diagnostics::default(),
            )
            .expect_err("non-empty output directories should be rejected, not replaced");
            assert!(fixture.js_out.join("stale.txt").exists());
            assert!(fixture.harness_out.join("stale.txt").exists());
            // Refused before any write: a later failure would leave `app/` or `reports/` behind.
            for out_dir in [&fixture.js_out, &fixture.harness_out] {
                assert!(!out_dir.join("app").exists(), "{err:#}");
                assert!(!out_dir.join("reports").exists(), "{err:#}");
            }
            Ok(())
        })
    }

    #[test]
    fn dry_run_runs_pipeline_checks_without_writing_outputs() -> Result<()> {
        js_ast::with_swc_globals(|| {
            let fixture = existing_outputs_fixture()?;
            run_transform(
                &fixture.request,
                TransformRunOptions::dry_run(),
                &analysis::diagnostics::Diagnostics::default(),
            )?;

            assert!(fixture.js_out.join("stale.txt").exists());
            assert!(fixture.harness_out.join("stale.txt").exists());
            assert!(!fixture.js_out.join("app").exists());
            assert!(!fixture.harness_out.join("app").exists());
            assert!(!fixture.js_out.join("reports").exists());
            assert!(!fixture.harness_out.join("reports").exists());
            Ok(())
        })
    }

    fn empty_transform_spec() -> TransformSpec {
        TransformSpec {
            host_behavior: spec::HostBehavior::default(),
            program_premises: spec::ProgramPremises::default(),
            inputs: spec::LoadJsChunksArgs {
                input_root: PathBuf::from("."),
                js_list_path: PathBuf::from("js-files.txt"),
                embedded_loaders_path: None,
            },
            vendor: BTreeMap::new(),
            logical_modules: BTreeMap::new(),
            import_order: BTreeMap::new(),
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
