use artifact::load_js_chunks;
use source_identity::ChunkId;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use anyhow::{Context, Result, bail};

use analysis_projection::AnalysisSpec;
use artifact::EmissionFiles;
use artifact::reports::{ArtifactChunkRecord, ChunkDecompositionOutput};
use emit_harness::emit_browser_harness;
use final_global_resolution::{GlobalResolutionSources, assess_final_global_resolution};
use gate::{CandidateRelocationAssessmentReport, UnmatchedSpecClaim};
use graph_provenance::{
    CurrentRoots, GraphProvenance, InputCapture, ProvenanceRoots, VerifiedGraphSourceContext,
};
use lowering::ownership_subset::OwnershipSubset;
use lowering::{
    AnalysisMode, CandidateEmissionDraft, FullReportKind, GateRejectionPolicy,
    GraphReportSelection, MaterializeLogicalModulesOptions, MaterializeSpecInputs, Materialized,
    ReportEmission, materialize_logical_modules,
};
use output_layout::GLOBAL_RESOLUTION_REPORT;
use prepare_chunks::{SourcePreparation, prepare_js_chunks};
use reconstruction::{EmittablePlan, ReconstructionPlan, RejectedPlan, VendorManifestOutput};
use spec::{ChunkExportPurity, LogicalModule, TransformSpec};
use spec_modules::read_flat_transform_spec;
use spec_tree::{CompileSpecTreeOptions, compile_spec_tree, graph_module_roots};
use vendor::{
    VendorPlanOptions, VendorResolutionPlan, VendorSwapsReport, build_partial_swap_resolutions,
    build_vendor_resolution_plan,
};
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

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TransformRequest {
    pub spec_source: TransformSpecSource,
    pub package_roots: HashMap<String, PathBuf>,
    pub packages_root: Option<PathBuf>,
}

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

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TransformSpecSource {
    Flat { path: PathBuf },
    Tree(CompileSpecTreeOptions),
}

pub fn run_transform(cli: &TransformRequest, options: TransformRunOptions) -> Result<()> {
    run_transform_with_analysis(cli, options, None)
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
) -> Result<()> {
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
    let (spec, prepared) =
        prepare_transform(cli, &options, None, analysis.map(|input| input.path))?;
    let mode = match analysis {
        Some(AnalysisArtifactInput {
            path,
            analysis_engine_sha256,
        }) => AnalysisMode::Load {
            base: Box::new(AnalysisSpec::from_transform(&spec, &prepared.source_root)?),
            artifact: fs::read(path)
                .with_context(|| format!("read analysis artifact {}", path.display()))?,
            analysis_engine_sha256: analysis_engine_sha256.to_string(),
        },
        None => AnalysisMode::Compute,
    };
    run_prepared_transform(&spec, prepared, options, false, mode, false)?;
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

    let options = audit_options();
    let (mut spec, prepared) = prepare_transform(cli, &options, None, Some(analysis.path))?;
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
    validate_transform_spec(&spec)?;

    let artifact = fs::read(analysis.path)
        .with_context(|| format!("read analysis artifact {}", analysis.path.display()))?;
    let mode = AnalysisMode::Load {
        base: Box::new(AnalysisSpec::from_transform(&spec, &prepared.source_root)?),
        artifact,
        analysis_engine_sha256: analysis.analysis_engine_sha256.to_string(),
    };
    let outcome = run_prepared_transform(&spec, prepared, options, true, mode, true)?;
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
pub fn project_analysis(cli: &TransformRequest, out: &Path) -> Result<()> {
    let options = audit_options();
    let (spec, prepared) = prepare_transform(cli, &options, None, None)?;
    let base = AnalysisSpec::from_transform(&spec, &prepared.source_root)?;
    let artifact = prepared.source.program.into_artifact();
    let projection =
        frontend_elaboration::project_analysis(artifact, &spec, &base, Some(&prepared.provenance))?;
    fs::write(out, projection.encode()?)
        .with_context(|| format!("write analysis projection {}", out.display()))
}

/// Input-only preparation shared by the baseline and independent contract probes.
/// Each run gets its own mutable AST; vendor decisions remain read-only.
#[derive(Clone)]
struct PreparedTransform {
    source: SourcePreparation,
    vendor_plan: Arc<VendorResolutionPlan>,
    provenance: GraphProvenance,
    /// Host loader per embedded input file.
    embedded_loaders: BTreeMap<String, spec::BunLoader>,
    /// The root the spec's source paths resolve against.
    source_root: PathBuf,
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
    pub fn prepare(cli: &TransformRequest, expected_spec: &TransformSpec) -> Result<Self> {
        let (spec, prepared) = prepare_transform(cli, &audit_options(), Some(expected_spec), None)?;
        run_prepared_transform(
            &spec,
            prepared.clone(),
            audit_options(),
            true,
            AnalysisMode::Compute,
            false,
        )?;
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
        let outcome = run_prepared_transform(
            &spec,
            self.prepared.clone(),
            audit_options(),
            true,
            AnalysisMode::Compute,
            false,
        );
        self.provenance()
            .verify(&CurrentRoots::default())
            .context("audit inputs changed between baseline and probe")?;
        Ok(match outcome {
            Ok(_) => ContractProbeOutcome::Accepted,
            Err(error) if error.chain().any(|cause| cause.is::<std::io::Error>()) => {
                ContractProbeOutcome::Inconclusive(error)
            }
            Err(error) => ContractProbeOutcome::Rejected(error),
        })
    }
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

fn audit_options() -> TransformRunOptions {
    TransformRunOptions {
        ownership_subset: None,
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

fn prepare_transform(
    cli: &TransformRequest,
    options: &TransformRunOptions,
    expected_spec: Option<&TransformSpec>,
    loaded_analysis: Option<&Path>,
) -> Result<(TransformSpec, PreparedTransform)> {
    let (roots, modules_directory) = provenance_roots(cli)?;
    let generation_command = canonical_generation_command(cli, options, &roots, loaded_analysis)?;
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
    let source_root = graph_provenance::absolute(&source_root)?;
    capture.set_source_root(&source_root)?;
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
            embedded_loaders,
            source_root,
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
) -> Result<PreparedTransformResult> {
    let PreparedTransform {
        source,
        vendor_plan,
        provenance,
        embedded_loaders,
        ..
    } = prepared;
    let SourcePreparation {
        program: source_program,
        counts,
        chunk_records,
    } = source;
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
    let mut vendor_report = VendorSwapsReport::default();
    let write_vendor_outputs = swap_cfg.write && write_program;

    // Full swaps are an emission-set exclusion: the swapped chunks stay in
    // the bundle — nothing removes them, and the dependency graph never
    // contained vendor chunks — but emission, the rename queue, the
    // emit-shape check, and the chunk reports skip them. Their
    // wire-facing resolutions are projections of the plan.
    vendor_report.full = vendor_plan.full_swap_resolutions();

    let mut module_count: usize = 0;
    let mut selected_lowerings: Vec<artifact::reports::SelectedModuleLowering> = Vec::new();
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
        module_count = materialize_result.module_count;
        selected_lowerings = materialize_result.selected_lowerings;
        unmatched_spec_claims = materialize_result.unmatched_spec_claims;
        gate_rejections = materialize_result.gate_rejections;
        decomposition_by_chunk = materialize_result.decomposition_by_chunk;
        materialize_result.output
    } else {
        CandidateEmissionDraft::pass_through(source_program, Arc::clone(&vendor_plan))
    };

    // The draft runs naturalization, vendor emission rewrites, and both
    // prune passes in their fixed order; its finalized files are immutable.
    let candidate = candidate_draft.finalize()?;
    // Report-only input; reconstruction consumes the candidate below.
    let global_use_elisions = candidate.global_use_elisions().clone();

    (vendor_report.partial, vendor_report.bundled_partial) =
        build_partial_swap_resolutions(&vendor_plan, candidate.references_by_symbol())?;

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
                chunk_records: &emitted_chunk_records(
                    chunk_records,
                    rejected_plan.files(),
                    rejected_plan.excluded_chunk_ids(),
                ),
                module_count,
                decomposition_by_chunk: &decomposition_by_chunk,
            })?;
        }
        return Err(GateRejected(rejected_plan.diagnostic()).into());
    }

    // Freeze the exact final syntax and replacement decisions before any
    // accepted-program output is written. Diagnostic reports may already exist.
    let validated_plan = ReconstructionPlan::new(
        candidate,
        unmatched_spec_claims,
        spec.emit_browser_harness.as_ref(),
        VendorManifestOutput::prepare(swap_cfg.output_manifest_path.as_deref(), vendor_report),
    )?
    .validate()?;
    let emission_files = validated_plan.files();
    let excluded_chunk_ids = validated_plan.excluded_chunk_ids();

    // Report-only: the legacy gate and final validation above stay the
    // acceptance authority.
    if !dry_run
        && !suppress_reports
        && let Some(report_dir) = &report_out_dir
    {
        let global_resolution = assess_final_global_resolution(
            &global_resolution_sources,
            emission_files.files(),
            excluded_chunk_ids,
            &global_use_elisions,
        );
        fs::create_dir_all(report_dir)
            .with_context(|| format!("create report directory {}", report_dir.display()))?;
        artifact::reports::write_json(
            report_dir.join(GLOBAL_RESOLUTION_REPORT),
            &global_resolution,
        )?;
    }

    // Vendor emission outputs: full-swap wrappers and bundled bundle
    // copies / facades, plus the combined manifest — all write-gated
    // behind `swap_vendor_chunks.write`.
    if write_vendor_outputs {
        validated_plan.write_vendor_outputs()?;
    }

    let emitted_chunk_records =
        emitted_chunk_records(chunk_records, emission_files, excluded_chunk_ids);

    if write_program && let Some(cfg) = &spec.write_js_tree {
        write_js_tree(&WriteTreeInput {
            plan: EmittablePlan::Accepted(&validated_plan),
            out_dir: &cfg.out_dir,
            lowerings: &selected_lowerings,
            counts: &counts,
            chunk_records: &emitted_chunk_records,
            module_count,
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

/// Chunk records of the emission set: records of excluded (fully-swapped)
/// chunks are dropped from the emitted reports.
fn emitted_chunk_records(
    chunk_records: Vec<ArtifactChunkRecord>,
    emission_files: &EmissionFiles,
    excluded_chunk_ids: &BTreeSet<ChunkId>,
) -> Vec<ArtifactChunkRecord> {
    let excluded_chunk_names: BTreeSet<&str> = excluded_chunk_ids
        .iter()
        .map(|chunk_id| emission_files.files().chunk_table.name(*chunk_id))
        .collect();
    chunk_records
        .into_iter()
        .filter(|record| !excluded_chunk_names.contains(record.chunk_id.as_str()))
        .collect()
}

fn provenance_roots(cli: &TransformRequest) -> Result<(ProvenanceRoots, Option<PathBuf>)> {
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
    cli: &TransformRequest,
    options: &TransformRunOptions,
    roots: &ProvenanceRoots,
    loaded_analysis: Option<&Path>,
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
        argv.extend(["--full-report".to_string(), report.as_str().to_string()]);
    }
    for chunk in &options.full_report_chunks {
        argv.extend(["--full-report-chunk".to_string(), chunk.clone()]);
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

/// Fold one application site's per-symbol rewrite counts into the
/// merged map `build_partial_swap_resolutions` projects from.
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

    preflight_roots(roots)
}

fn preflight_roots(mut roots: Vec<PathBuf>) -> Result<()> {
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

    const KEEP_GOING_RUN: TransformRunOptions = TransformRunOptions {
        ownership_subset: None,
        output: RunOutput::Accepted,
        artifacts: RunArtifacts::Program,
        graph_reports: GraphReportSelection::None,
        keep_going: true,
        report_dir_override: None,
        list_template_identifiers: false,
        effect_conflicts_jsonl: false,
        effect_conflicts_chunks: BTreeSet::new(),
        full_reports: BTreeSet::new(),
        full_report_chunks: BTreeSet::new(),
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
            let cli = TransformRequest {
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

            run_transform(
                &TransformRequest {
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

            let err = run_transform(
                &TransformRequest {
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

            run_transform(
                &TransformRequest {
                    spec_source: TransformSpecSource::Flat { path: spec_path },
                    package_roots: HashMap::new(),
                    packages_root: None,
                },
                TransformRunOptions {
                    ownership_subset: None,
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
            host_behavior: spec::HostBehavior::default(),
            program_premises: spec::ProgramPremises::default(),
            inputs: spec::LoadJsChunksArgs {
                input_root: PathBuf::from("."),
                js_list_path: PathBuf::from("js-files.txt"),
                embedded_loaders_path: None,
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
