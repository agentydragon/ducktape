//! Apply explicit source-unit assignments without transient name-pinned YAML.

use graph_provenance::VerifiedGraphSourceContext;
use std::fs;
use std::io::Read;
use std::path::PathBuf;

use anyhow::{Context, Result};
use clap::Args;
use peel::OutputFormat;
use spec_tree::ModuleTreeScope;

use crate::binding::load_module_docs;
use crate::candidate_assessment::CandidateEffectAssessmentExistingTreeArgs;
use crate::edit_gate::GraphEditArgs;
use crate::outcome::{MutationVerb, emit_gate_rejection_json};

#[derive(Debug, Args)]
pub struct ModulesApplyArgs {
    /// JSON array of exact {unit_id, module} changes, including residual and anonymous units.
    #[arg(long, required_unless_present = "plan", conflicts_with = "plan")]
    assignments: Option<String>,
    /// Saved exact plan from modules propose/check, verified against the current graph.
    #[arg(long, conflicts_with = "assignments")]
    plan: Option<PathBuf>,
    /// JSON map from newly claimed source binding names to readable names.
    #[arg(long)]
    readable_names: Option<PathBuf>,
    /// Root directory of per-module YAML files.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    pub(super) modules_root: PathBuf,
    /// Tree config that scopes this graph-backed edit to its chunk's module roots.
    #[arg(long = "tree-config", env = "DEBUNDLE_TREE_CONFIG")]
    pub(super) tree_config: Option<PathBuf>,
    /// Current dependency graph, used to check the resulting assignment.
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

pub fn run_modules_apply(
    output: &mut dyn std::io::Write,
    stderr: &mut (dyn std::io::Write + Send),
    args: ModulesApplyArgs,
    source_context: Option<&VerifiedGraphSourceContext>,
    analysis_engine_sha256: &'static str,
) -> Result<()> {
    let edit = GraphEditArgs {
        modules_root: args.modules_root.clone(),
        dry_run: args.dry_run,
        no_verify: false,
        dependency_graph_path: Some(args.graph.clone()),
        source_root: Some(args.source_root.clone()),
        format: args.format,
    };
    let effect_context = args.effect_assessment.resolve_for_verified_write(
        &edit,
        source_context,
        args.tree_config.as_deref(),
    )?;
    anyhow::ensure!(
        args.readable_names.is_none() || args.assignments.is_some() || args.plan.is_some(),
        "--readable-names requires --assignments or --plan"
    );
    {
        let assignments = if let Some(path) = &args.plan {
            let plan: peel::assignment::AssignmentPlan = serde_json::from_slice(&fs::read(path)?)?;
            plan.verify(&args.graph)?;
            physical_plan_changes(plan.changes, &args)?
        } else {
            serde_json::from_str(&read_batch(
                args.assignments.as_deref().expect("assignment mode"),
            )?)
            .context("parsing source-unit assignment batch")?
        };
        let readable_names = args
            .readable_names
            .as_ref()
            .map(|path| serde_json::from_slice(&fs::read(path)?).context("parsing readable names"))
            .transpose()?
            .unwrap_or_default();
        let effect_assessment_context = effect_context.as_ref().map(|context| {
            crate::binding::CandidateEffectAssessmentContext {
                transform: &context.transform,
                analysis: pipeline::AnalysisArtifactInput {
                    path: &context.analysis_path,
                    analysis_engine_sha256,
                },
                graph_source: context.graph_source,
            }
        });
        let outcome = crate::source_unit_refine::run_source_unit_refine(
            stderr,
            &args.modules_root,
            &args.graph,
            &args.source_root,
            source_context,
            crate::source_unit_refine::SourceUnitRefineOptions {
                tree_config: args.tree_config.as_deref(),
                assignments,
                verb: MutationVerb::Apply,
                readable_names,
                dry_run: args.dry_run,
                effect_assessment_context,
            },
        )
        .map_err(|err| emit_gate_rejection_json(output, MutationVerb::Apply, args.format, err))?;
        outcome.outcome.warn_if_unassessed_write(stderr)?;
        command_output::write_report(
            &outcome,
            OutputFormat::resolve(args.format),
            output,
            |out, text| {
                use std::fmt::Write;
                writeln!(
                    text,
                    "{}: {} residual source unit(s) claimed, {} existing unit(s) moved",
                    out.outcome.action,
                    out.residual_source_units_claimed,
                    out.source_units_reassigned
                )?;
                writeln!(text, "{}", serde_yaml::to_string(&out.synthesized_claims)?)?;
                Ok(())
            },
        )?;
        Ok(())
    }
}

fn physical_plan_changes(
    mut changes: Vec<peel::assignment::SourceUnitAssignment>,
    args: &ModulesApplyArgs,
) -> Result<Vec<peel::assignment::SourceUnitAssignment>> {
    let Some(config) = &args.tree_config else {
        return Ok(changes);
    };
    let input = analysis::DependencyGraphInput::read(&args.graph)?;
    let scope = ModuleTreeScope::from_config(config, &input.report().chunk_id)?;
    let docs = load_module_docs(&args.modules_root)?;
    for change in &mut changes {
        let logical = spec::ModulePath::parse(&change.module, "")?;
        let mut existing = Vec::new();
        for physical in docs
            .keys()
            .filter(|path| scope.contains(std::path::Path::new(path)))
        {
            if spec::ModulePath::parse(scope.logical_destination(physical)?, "")? == logical {
                existing.push(physical.clone());
            }
        }
        anyhow::ensure!(
            existing.len() <= 1,
            "logical destination {logical} has multiple authored files"
        );
        change.module = if let Some(existing) = existing.pop() {
            existing
        } else {
            scope.roots()[0]
                .join(logical.as_str())
                .to_str()
                .context("module destination is not UTF-8")?
                .to_string()
        };
    }
    Ok(changes)
}

fn read_batch(path: &str) -> Result<String> {
    Ok(if path == "-" {
        let mut text = String::new();
        std::io::stdin().read_to_string(&mut text)?;
        text
    } else {
        fs::read_to_string(path).with_context(|| format!("reading {path}"))?
    })
}
