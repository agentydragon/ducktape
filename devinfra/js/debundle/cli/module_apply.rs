//! Apply exact source-unit assignments after a full graph check.

use graph_provenance::VerifiedGraphSourceContext;
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use clap::Args;
use peel::OutputFormat;
use peel::assignment::{AssignmentPlan, SourceUnitAssignment};
use serde_json::from_slice;
use spec::ModulePath;
use spec_tree::ModuleTreeScope;

use crate::binding::load_module_docs;
use crate::candidate_assessment::{
    CandidateEffectAssessmentExistingTreeArgs, EffectAssessmentUnavailable,
};
use crate::edit_gate::GraphEditArgs;
use crate::outcome::{MutationVerb, emit_gate_rejection_json};
use crate::source_unit_apply::{SourceUnitApplyOptions, run_source_unit_apply};

#[derive(Debug, Args)]
pub struct ModulesApplyArgs {
    /// JSON array of exact {unit_id, module} changes; `-` reads stdin.
    #[arg(long, required_unless_present = "plan", conflicts_with = "plan")]
    assignments: Option<String>,
    /// Saved exact plan from `modules propose` or `modules check`.
    #[arg(long, conflicts_with = "assignments")]
    plan: Option<PathBuf>,
    /// Complete JSON array of nonentry module paths for an explicit batch.
    #[arg(long = "import-order", conflicts_with = "plan")]
    import_order: Option<PathBuf>,
    /// JSON map from newly claimed binding names to readable names.
    #[arg(long)]
    readable_names: Option<PathBuf>,
    /// JSON object keyed by source-unit ID, then binding name, with reviewed selectors.
    #[arg(long = "selector-overrides")]
    selector_overrides: Option<PathBuf>,
    /// Root directory of per-module YAML files.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    pub(super) modules_root: PathBuf,
    /// Tree config that scopes this graph-backed edit to its chunk's module roots.
    #[arg(long = "tree-config", env = "DEBUNDLE_TREE_CONFIG")]
    pub(super) tree_config: Option<PathBuf>,
    /// Current dependency graph used to check the resulting assignment.
    #[arg(long = "graph", env = "DEBUNDLE_GRAPH")]
    pub(super) graph: PathBuf,
    /// Root for the chunk's source file.
    #[arg(long = "source-root", env = "DEBUNDLE_SOURCE_ROOT")]
    pub(super) source_root: PathBuf,
    /// Report-only relocation assessment of the exact candidate before an
    /// optional write, using a staged `--analysis` artifact.
    #[command(flatten)]
    effect_assessment: CandidateEffectAssessmentExistingTreeArgs,
    /// Validate and show the edit without writing YAML.
    #[arg(long)]
    pub(super) dry_run: bool,
    /// Output format. Default text on tty, JSON on pipe.
    #[arg(long, value_enum)]
    pub(super) format: Option<OutputFormat>,
}

#[derive(Debug, serde::Serialize)]
struct ApplyOutcome {
    #[serde(flatten)]
    outcome: crate::source_unit_apply::SourceUnitApplyOutcome,
    /// Why the canonical effect verdict was unavailable for this graph-backed edit.
    #[serde(skip_serializing_if = "Option::is_none")]
    effect_assessment_unavailable: Option<EffectAssessmentUnavailable>,
}

pub fn run_modules_apply(
    output: &mut dyn std::io::Write,
    stderr: &mut (dyn std::io::Write + Send),
    args: ModulesApplyArgs,
    source_context: Option<&VerifiedGraphSourceContext>,
    analysis_engine_sha256: &'static str,
) -> Result<()> {
    anyhow::ensure!(
        args.readable_names.is_none() || args.assignments.is_some() || args.plan.is_some(),
        "--readable-names requires --assignments or --plan"
    );
    anyhow::ensure!(
        args.selector_overrides.is_none() || args.assignments.is_some() || args.plan.is_some(),
        "--selector-overrides requires --assignments or --plan"
    );
    let edit = GraphEditArgs {
        modules_root: args.modules_root.clone(),
        dry_run: args.dry_run,
        no_verify: false,
        dependency_graph_path: Some(args.graph.clone()),
        source_root: Some(args.source_root.clone()),
        import_order: None,
        format: args.format,
    };
    let effect_context = args.effect_assessment.resolve_for_verified_write(
        &edit,
        source_context,
        args.tree_config.as_deref(),
        analysis_engine_sha256,
    )?;
    let (mut assignments, import_order) = read_assignment_request(
        &args.graph,
        args.plan.as_deref(),
        args.assignments.as_deref(),
        args.import_order.as_deref(),
    )?;
    if args.plan.is_some() {
        let input = analysis::DependencyGraphInput::read(&args.graph)?;
        assignments = physical_plan_changes(
            assignments,
            &input,
            args.tree_config.as_deref(),
            &args.modules_root,
            source_context,
        )?;
    }
    let readable_names = args
        .readable_names
        .as_ref()
        .map(|path| from_slice(&fs::read(path)?).context("parsing readable names"))
        .transpose()?
        .unwrap_or_default();
    let selector_overrides = args
        .selector_overrides
        .as_ref()
        .map(|path| from_slice(&fs::read(path)?).context("parsing selector overrides"))
        .transpose()?
        .unwrap_or_default();
    let outcome = run_source_unit_apply(
        stderr,
        &args.modules_root,
        &args.graph,
        &args.source_root,
        source_context,
        SourceUnitApplyOptions {
            tree_config: args.tree_config.as_deref(),
            assignments,
            import_order,
            readable_names,
            selector_overrides,
            dry_run: args.dry_run,
            effect_assessment_context: effect_context.as_ref(),
        },
    )
    .map_err(|err| emit_gate_rejection_json(output, MutationVerb::Apply, args.format, err))?;
    outcome.outcome.warn_if_unassessed_write(stderr)?;
    let report = ApplyOutcome {
        effect_assessment_unavailable: outcome
            .effect_relocation_assessment
            .is_none()
            .then_some(EffectAssessmentUnavailable::AnalysisNotSupplied),
        outcome,
    };
    command_output::write_report(
        &report,
        OutputFormat::resolve(args.format),
        output,
        |out, text| {
            use std::fmt::Write;
            writeln!(
                text,
                "{}: {} residual source unit(s) claimed, {} existing unit(s) moved",
                out.outcome.outcome.action,
                out.outcome.residual_source_units_claimed,
                out.outcome.source_units_reassigned
            )?;
            writeln!(
                text,
                "{}",
                serde_yaml::to_string(&out.outcome.synthesized_claims)?
            )?;
            Ok(())
        },
    )?;
    Ok(())
}

/// Resolve the chunk-logical destinations shared by plans and checks to the
/// exact authoring locations the source writer will use.
pub(super) fn physical_plan_changes(
    mut changes: Vec<peel::assignment::SourceUnitAssignment>,
    input: &analysis::DependencyGraphInput,
    tree_config: Option<&Path>,
    modules_root: &Path,
    source_context: Option<&VerifiedGraphSourceContext>,
) -> Result<Vec<peel::assignment::SourceUnitAssignment>> {
    let Some(config) = tree_config else {
        return Ok(changes);
    };
    if input.report().ownership_subset.is_some() {
        // Selected-view claims are stored under chunk-logical module keys,
        // independent of the immutable base tree's physical YAML roots.
        return Ok(changes);
    }
    let scope = ModuleTreeScope::from_config(config, &input.report().chunk_id)?;
    let modules_root = source_context
        .and_then(VerifiedGraphSourceContext::modules_root)
        .unwrap_or(modules_root);
    let docs = load_module_docs(modules_root)?;
    let mut authored_paths = std::collections::BTreeMap::<_, Vec<_>>::new();
    for physical in docs.keys().filter(|path| scope.contains(Path::new(path))) {
        let logical = spec::ModulePath::parse(scope.logical_destination(physical)?, "")?;
        authored_paths.entry(logical).or_default().push(physical);
    }
    for change in &mut changes {
        let logical = spec::ModulePath::parse(&change.module, "")?;
        let existing = authored_paths
            .get(&logical)
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        anyhow::ensure!(
            existing.len() <= 1,
            "logical destination {logical} has multiple authored files"
        );
        change.module = if let Some(existing) = existing.first() {
            (*existing).clone()
        } else {
            scope
                .roots()
                .first()
                .context("tree config has no module root for plan destination")?
                .join(logical.as_str())
                .to_str()
                .context("module destination is not UTF-8")?
                .to_string()
        };
    }
    Ok(changes)
}

pub(super) fn read_assignment_request(
    graph: &Path,
    plan: Option<&Path>,
    batch: Option<&str>,
    import_order: Option<&Path>,
) -> Result<(Vec<SourceUnitAssignment>, Option<Vec<ModulePath>>)> {
    if let Some(path) = plan {
        let plan: AssignmentPlan = from_slice(&fs::read(path)?)?;
        plan.verify(graph)?;
        return Ok((plan.changes, Some(plan.import_order)));
    }
    let batch = batch.expect("clap requires a batch or plan");
    let bytes = if batch == "-" {
        let mut bytes = Vec::new();
        std::io::Read::read_to_end(&mut std::io::stdin(), &mut bytes)?;
        bytes
    } else {
        fs::read(batch).with_context(|| format!("reading {batch}"))?
    };
    let changes = from_slice(&bytes).context("parsing source-unit assignment batch")?;
    let import_order = import_order
        .map(|path| {
            from_slice(&fs::read(path)?)
                .with_context(|| format!("parsing import order {}", path.display()))
        })
        .transpose()?;
    Ok((changes, import_order))
}
