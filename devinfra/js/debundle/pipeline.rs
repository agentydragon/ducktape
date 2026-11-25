#[path = "apply_projection_codec.rs"]
mod apply_projection_codec;
mod resolved_placement;
pub use resolved_placement::{ProposalFragment, resolve_placement};

use artifact::load_js_chunks;
use serde::{Deserialize, Serialize};
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
use lowering::ownership_subset::OwnershipSubset;
use lowering::{
    AnalysisMode, CandidateEmissionDraft, FullReportKind, GateRejectionPolicy,
    GraphReportSelection, MaterializeLogicalModulesOptions, MaterializeSpecInputs, Materialized,
    ReportEmission, materialize_logical_modules,
};
use output_layout::GLOBAL_RESOLUTION_REPORT;
use prepare_chunks::prepare_js_chunks;
use reconstruction::{EmittablePlan, RejectedPlan, VendorManifestOutput, validate_reconstruction};
use spec::{ChunkExportPurity, LogicalModule, NodeBuiltinModuleExt, TransformSpec};
use spec_modules::read_flat_transform_spec;
use spec_tree::{
    CompileSpecTreeOptions, compile_spec_tree, compile_spec_tree_with_roots, graph_module_roots,
};
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

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OwnershipSubsetInput {
    pub path: PathBuf,
    pub bytes: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TransformSpecSource {
    Flat { path: PathBuf },
    Tree(CompileSpecTreeOptions),
}

/// Canonical input for apply runs that do not export replayable graphs.
/// Retain authored paths for report locations and every report/validation note.
/// Only module-level `note` is unused by the transform pipeline.
#[derive(Debug)]
struct ApplyProjection {
    source: TransformSpecSource,
    spec: TransformSpec,
    module_roots_by_chunk: BTreeMap<String, Vec<PathBuf>>,
    rooted_paths: BTreeSet<ApplyPath>,
}

const APPLY_OUTPUT_ROOT: &str = "__debundle_apply_output_root__";

/// Tree compilation creates these paths from `out_root`. Keep their suffixes
/// in the projection and bind them to the consuming target's output root.
/// Authored source paths remain subject to the ordinary projection-root mapping.
fn map_tree_output_paths(spec: &mut TransformSpec, from: &Path, to: &Path) -> Result<()> {
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
enum ApplyPath {
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
    let spec = load_transform_spec_source(&cli.spec_source)?;
    project_apply_from_spec(cli, root, out, spec, None)
}

fn project_apply_from_spec(
    cli: &TransformRequest,
    root: &Path,
    out: &Path,
    mut spec: TransformSpec,
    compiled_roots: Option<&BTreeMap<String, Vec<PathBuf>>>,
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
    let module_roots_by_chunk = match (&cli.spec_source, compiled_roots) {
        (TransformSpecSource::Tree(_), Some(roots)) => roots.clone(),
        (TransformSpecSource::Tree(tree), None) => graph_module_roots(&tree.config_path)?,
        (TransformSpecSource::Flat { .. }, _) => BTreeMap::new(),
    };
    let root = std::path::absolute(root)?;
    let relative = |path: &Path| -> Result<PathBuf> {
        let path = std::path::absolute(path)?;
        Ok(path.strip_prefix(&root).unwrap_or(&path).to_path_buf())
    };
    let mut source = cli.spec_source.clone();
    map_spec_source_paths(&mut source, &relative)?;
    if let TransformSpecSource::Tree(tree) = &mut source {
        tree.out_root = PathBuf::from(APPLY_OUTPUT_ROOT);
    }
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

fn map_spec_source_paths(
    source: &mut TransformSpecSource,
    map: &impl Fn(&Path) -> Result<PathBuf>,
) -> Result<()> {
    match source {
        TransformSpecSource::Flat { path } => *path = map(path)?,
        TransformSpecSource::Tree(tree) => {
            tree.config_path = map(&tree.config_path)?;
            tree.modules_root = map(&tree.modules_root)?;
            tree.vendor_marks_path = map(&tree.vendor_marks_path)?;
            tree.out_root = map(&tree.out_root)?;
            if let Some(root) = &mut tree.source_root {
                *root = map(root)?;
            }
        }
    }
    Ok(())
}

/// Filesystem paths only: module IDs and specifiers are semantic strings.
fn map_apply_spec_paths(
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
        if let spec::VendorLevel::BundledPartialSwap(swap) = &mut mark.level {
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
            let mut source = cli.spec_source.clone();
            map_spec_source_paths(&mut source, &|path| {
                let path = std::path::absolute(path)?;
                Ok(path.strip_prefix(&root).unwrap_or(&path).to_path_buf())
            })?;
            if let TransformSpecSource::Tree(tree) = &mut source {
                tree.out_root = PathBuf::from(APPLY_OUTPUT_ROOT);
            }
            anyhow::ensure!(
                projection.source == source,
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
            base: Box::new(AnalysisSpec::from_transform(&spec, &prepared.source_root)?),
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

    let options = audit_options();
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
        base: Box::new(AnalysisSpec::from_transform(&spec, &prepared.source_root)?),
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
    let options = audit_options();
    let (spec, prepared) = prepare_transform(cli, &options, None, None, None)?;
    if let Some((root, out)) = apply_out {
        project_apply_from_spec(
            cli,
            root,
            out,
            spec.clone(),
            Some(&prepared.module_roots_by_chunk),
        )?;
    }
    let base = AnalysisSpec::from_transform(&spec, &prepared.source_root)?;
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
    /// The root the spec's source paths resolve against.
    source_root: PathBuf,
    module_roots_by_chunk: BTreeMap<String, Vec<PathBuf>>,
}

/// Exact capture identity used to group selected corpus rows. Reuse is local
/// to one command and requires the complete provenance, relocated run context,
/// root mapping, and verified selected-view bytes to agree.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct SelectedOwnershipProgramKey(String);

/// One immutable source/spec preparation shared by selected-view edits from a
/// compatible capture. Semantic value-flow analysis comes from the saved graph;
/// source parsing and the full-base frontend are prepared once per group.
pub struct SelectedOwnershipProgram {
    spec: TransformSpec,
    prepared: PreparedTransform,
    key: SelectedOwnershipProgramKey,
    sources: BTreeMap<String, (PathBuf, String)>,
    baseline_placement_chunks: BTreeSet<String>,
}

impl SelectedOwnershipProgram {
    pub fn key(
        graph_source: &VerifiedGraphSourceContext,
        provenance: &GraphProvenance,
    ) -> Result<SelectedOwnershipProgramKey> {
        let roots = graph_source.current_roots();
        let (path, bytes) = graph_source.ownership_subset_input().context(
            "selected-view editing requires the graph's captured ownership-subset input",
        )?;
        let encoded = serde_json::to_vec(&(
            provenance,
            graph_source.regeneration_command(),
            &roots.spec,
            &roots.source,
            &roots.ownership_subset,
            &roots.packages,
            path,
            graph_provenance::sha256(bytes),
        ))?;
        Ok(SelectedOwnershipProgramKey(graph_provenance::sha256(
            &encoded,
        )))
    }

    pub fn prepare(
        request: &TransformRequest,
        graph_source: &VerifiedGraphSourceContext,
        provenance: &GraphProvenance,
    ) -> Result<Self> {
        let key = Self::key(graph_source, provenance)?;
        let (path, bytes) = graph_source.ownership_subset_input().context(
            "selected-view editing requires the graph's captured ownership-subset input",
        )?;
        let baseline: OwnershipSubset = serde_json::from_slice(bytes)
            .context("parsing the graph's verified ownership-subset input")?;
        let baseline_placement_chunks = baseline
            .placements
            .iter()
            .filter(|(_, modules)| !modules.is_empty())
            .map(|(chunk, _)| chunk.clone())
            .collect();
        let mut options = audit_options();
        options.ownership_subset = Some(baseline);
        options.ownership_subset_input = Some(OwnershipSubsetInput {
            path: path.to_path_buf(),
            bytes: bytes.to_vec(),
        });
        let (spec, prepared) = prepare_transform(request, &options, None, None, None)?;
        // Compare the bytes actually consumed, not a later reread that could
        // hide an intervening edit. The shared AST is this verified snapshot.
        let fingerprints = |provenance: &GraphProvenance, roots: &CurrentRoots| {
            provenance
                .files
                .iter()
                .map(|file| {
                    Ok((
                        graph_provenance::absolute(
                            &provenance.resolve(&file.root, &file.path, roots)?,
                        )?,
                        file.sha256.clone(),
                    ))
                })
                .collect::<Result<BTreeMap<_, _>>>()
        };
        anyhow::ensure!(
            fingerprints(provenance, graph_source.current_roots())?
                == fingerprints(&prepared.provenance, &CurrentRoots::default())?,
            "selected-view preparation consumed inputs different from the verified graph; regenerate the graph"
        );
        let sources = prepared
            .provenance
            .chunk_sources
            .iter()
            .map(|source| {
                (
                    source.chunk_id.clone(),
                    (source.source_path.clone(), source.sha256.clone()),
                )
            })
            .collect();
        let program = Self {
            spec,
            prepared,
            key,
            sources,
            baseline_placement_chunks,
        };
        program.verify_context(graph_source, provenance)?;
        Ok(program)
    }

    /// Each row retains its own verified context. A common chunk name alone
    /// cannot authorize reuse of another capture's AST, premises or selectors.
    pub fn verify_context(
        &self,
        graph_source: &VerifiedGraphSourceContext,
        provenance: &GraphProvenance,
    ) -> Result<()> {
        anyhow::ensure!(
            Self::key(graph_source, provenance)? == self.key,
            "selected-view graph inputs are incompatible with the prepared program"
        );
        let (path, sha256) = self
            .sources
            .get(graph_source.chunk_id())
            .context("selected-view transform context lacks the graph's chunk source")?;
        anyhow::ensure!(
            path == graph_source.source_path() && sha256 == graph_source.sha256(),
            "selected-view transform source differs from the verified graph"
        );
        Ok(())
    }

    /// Borrow one shared source/base frontend for a group. The scoped session
    /// owns resolved chunks and consumes each target once, without a cloned
    /// whole-program AST or another joint selector solve per candidate.
    pub fn with_preflight_session<R>(
        &self,
        mut placement_chunks: BTreeSet<String>,
        diagnostics: &analysis::diagnostics::Diagnostics<'_>,
        operation: impl FnOnce(&mut lowering::SelectedOwnershipPreflightSession<'_>) -> Result<R>,
    ) -> Result<R> {
        placement_chunks.extend(self.baseline_placement_chunks.iter().cloned());
        let chunks: Vec<_> = self.spec.materialize_chunk_ids().into_iter().collect();
        lowering::with_selected_ownership_preflight(
            lowering::SelectedOwnershipPreparationInput {
                artifact: self.prepared.source.artifact(),
                spec_inputs: MaterializeSpecInputs {
                    host_behavior: &self.spec.host_behavior,
                    program_premises: &self.spec.program_premises,
                    embedded_loaders: &self.prepared.embedded_loaders,
                    logical_modules: &self.spec.logical_modules,
                    chunk_renames: &self.spec.chunk_renames,
                    unassigned_mode: &self.spec.unassigned_mode,
                    import_order: &self.spec.import_order,
                    chunk_analysis_options: &self.spec.chunk_analysis_options,
                    chunk_export_purity: &self.spec.chunk_export_purity,
                },
                materialize_config: &self.spec.materialize_logical_modules,
                materialized_chunk_ids: &chunks,
                placement_chunks,
                diagnostics,
                graph_provenance: Some(&self.prepared.provenance),
            },
            operation,
        )
    }
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
    pub fn prepare(
        cli: &TransformRequest,
        expected_spec: &TransformSpec,
        diagnostics: &analysis::diagnostics::Diagnostics,
    ) -> Result<Self> {
        let (spec, prepared) =
            prepare_transform(cli, &audit_options(), Some(expected_spec), None, None)?;
        run_prepared_transform(
            &spec,
            prepared.clone(),
            audit_options(),
            true,
            AnalysisMode::Compute,
            false,
            diagnostics,
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
        diagnostics: &analysis::diagnostics::Diagnostics,
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
            diagnostics,
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

/// One prepared input for `spec lint --probe-analysis`: each probe withholds
/// one authored annotation from a copy of the spec and recomputes analysis,
/// graph, gate and reconstruction on the same prepared input, writing its
/// reports and full graphs under its own root for comparison.
pub struct AnnotationProbe {
    spec: TransformSpec,
    prepared: PreparedTransform,
}

/// How one probe run ended.
#[derive(Debug)]
pub enum AnnotationProbeRun {
    /// Final acceptance passed; reports are complete under the run's root.
    Accepted,
    /// The run failed (a gate rejection, an analysis or reconstruction error).
    Rejected(anyhow::Error),
    /// The probe itself could not run (an invalid edited spec, an I/O error).
    Inconclusive(anyhow::Error),
}

impl AnnotationProbe {
    pub fn prepare(cli: &TransformRequest, expected_spec: &TransformSpec) -> Result<Self> {
        let (spec, prepared) =
            prepare_transform(cli, &audit_options(), Some(expected_spec), None, None)?;
        Ok(Self { spec, prepared })
    }

    /// The spec the input was prepared for; probes edit copies of it.
    pub fn spec(&self) -> &TransformSpec {
        &self.spec
    }

    /// Run `spec` on the prepared input with reports and full graphs under
    /// `out_root`. Input changes since preparation abort the probe.
    pub fn run(
        &self,
        spec: &TransformSpec,
        out_root: &Path,
        diagnostics: &analysis::diagnostics::Diagnostics,
    ) -> Result<AnnotationProbeRun> {
        self.prepared
            .provenance
            .verify(&CurrentRoots::default())
            .context("probe inputs changed after preparation")?;
        if let Err(error) = validate_transform_spec(spec) {
            return Ok(AnnotationProbeRun::Inconclusive(error));
        }
        let outcome = run_prepared_transform(
            spec,
            self.prepared.clone(),
            TransformRunOptions {
                output: RunOutput::Accepted,
                artifacts: RunArtifacts::ReportsOnly {
                    out_root: out_root.to_path_buf(),
                },
                graph_reports: GraphReportSelection::Full,
                ..audit_options()
            },
            false,
            AnalysisMode::Compute,
            false,
            diagnostics,
        );
        self.prepared
            .provenance
            .verify(&CurrentRoots::default())
            .context("probe inputs changed during a probe")?;
        Ok(match outcome {
            Ok(_) => AnnotationProbeRun::Accepted,
            Err(error) if error.chain().any(|cause| cause.is::<std::io::Error>()) => {
                AnnotationProbeRun::Inconclusive(error)
            }
            Err(error) => AnnotationProbeRun::Rejected(error),
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
    let (spec, module_roots_by_chunk) = match (projection, &cli.spec_source) {
        (Some(projection), _) => (
            projection.spec.clone(),
            projection.module_roots_by_chunk.clone(),
        ),
        (None, TransformSpecSource::Tree(tree)) => compile_spec_tree_with_roots(tree)?,
        (None, TransformSpecSource::Flat { .. }) => (
            load_transform_spec_source(&cli.spec_source)?,
            BTreeMap::new(),
        ),
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
        capture.set_tree_scope(&tree.config_path, module_roots_by_chunk.clone())?;
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
            source_root,
            module_roots_by_chunk,
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
fn canonical_generation_command(
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
        ownership_subset_input: None,
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
                KEEP_GOING_RUN,
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
                KEEP_GOING_RUN,
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
                TransformRunOptions {
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
                },
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
