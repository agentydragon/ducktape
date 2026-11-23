//! `modules` command arguments, dispatch, and report presentation. Merge and
//! delete planning/persistence live in `module`, separate from CLI rendering.
use crate::comment::{ModuleCommentArgs, run_module_comment_cmd};
use crate::emit_report;
use crate::module::{DeleteArgs, MergeArgs, run_delete, run_merge};
use crate::module_apply::{
    ModulesApplyArgs, physical_plan_changes, read_assignment_request, run_modules_apply,
};
use crate::module_proposals::{GraphCorpusScope, discover_graphs, scope_graphs_to_tree};
use crate::source_unit_apply::{SourceUnitApplyOptions, run_source_unit_apply};
use anyhow::{Context, Result, ensure};
use clap::{Args as ClapArgs, Subcommand};
use graph_provenance::{CurrentRoots, VerifiedGraphSourceContext};
use peel::assignment::{
    AssignmentPlan, AssignmentProblem, AssignmentSnapshot, SourceUnitAssignment, accepted,
};
use peel::{CommonArgs, OutputFormat, ProposeArgs};
use std::fs;
use std::path::{Path, PathBuf};

/// Args for `debundle modules ...`. Aggregates the existing
/// comment-edit, merge, delete, propose, check, apply and list verbs.
#[derive(Debug, ClapArgs)]
pub(super) struct ModulesNs {
    #[command(subcommand)]
    command: ModulesNsCommand,
}

impl ModulesNs {
    pub(super) fn supports_selected_ownership_edit(&self) -> bool {
        matches!(&self.command, ModulesNsCommand::Apply(_))
    }
    pub(super) fn edits_with_graph(&self) -> bool {
        match &self.command {
            ModulesNsCommand::Merge(args) => args.edit.dependency_graph_path.is_some(),
            ModulesNsCommand::Delete(args) => args.edit.dependency_graph_path.is_some(),
            ModulesNsCommand::Apply(_) => true,
            _ => false,
        }
    }
    pub(super) fn writes_with_graph(&self) -> bool {
        match &self.command {
            ModulesNsCommand::Merge(args) => {
                args.edit.dependency_graph_path.is_some() && !args.edit.dry_run
            }
            ModulesNsCommand::Delete(args) => {
                args.edit.dependency_graph_path.is_some() && !args.edit.dry_run
            }
            ModulesNsCommand::Apply(args) => !args.dry_run,
            _ => false,
        }
    }
    pub(super) fn format(&self) -> Option<OutputFormat> {
        match &self.command {
            ModulesNsCommand::Merge(args) => args.edit.format,
            ModulesNsCommand::Delete(args) => args.edit.format,
            ModulesNsCommand::Propose(args) => args.format,
            ModulesNsCommand::Check(args) => args.format,
            ModulesNsCommand::Apply(args) => args.format,
            _ => None,
        }
    }
    pub(super) fn verify_graph(
        &self,
        roots: &CurrentRoots,
    ) -> Result<crate::VerifiedGraphContexts> {
        if let ModulesNsCommand::Propose(args) = &self.command {
            return verify_proposal_graphs(args, roots);
        }
        let context = match &self.command {
            ModulesNsCommand::Merge(args) => args.edit.verify_graph(roots),
            ModulesNsCommand::Delete(args) => args.edit.verify_graph(roots),
            ModulesNsCommand::Check(args) => crate::verify_graph_compatibility(
                &args.common.dependency_graph_path,
                &args.common.modules_root,
                args.source_root.as_deref(),
                roots,
                args.tree_config.as_deref(),
            )
            .map(Some),
            ModulesNsCommand::Apply(args) => crate::verify_graph_compatibility(
                &args.graph,
                &args.modules_root,
                Some(&args.source_root),
                roots,
                args.tree_config.as_deref(),
            )
            .map(Some),
            _ => Ok(None),
        }?;
        Ok(crate::single_graph_context(context))
    }
}

fn verify_proposal_graphs(
    args: &ProposeArgs,
    roots: &CurrentRoots,
) -> Result<crate::VerifiedGraphContexts> {
    if let Some(graph) = &args.dependency_graph_path {
        return crate::verify_graph_compatibility(
            graph,
            &args.modules_root,
            args.source_root.as_deref(),
            roots,
            args.tree_config.as_deref(),
        )
        .map(|context| crate::single_graph_context(Some(context)));
    }

    let reports_root = args
        .graphs_root
        .as_deref()
        .context("one of --graph or --graphs is required")?;
    let graphs = discover_graphs(reports_root)?;

    // Check current input provenance for the entire corpus before producing
    // any report. Out-of-scope chunks still need current graph/source evidence.
    for graph in graphs.values() {
        crate::verify_graph_compatibility_with_scope(
            graph,
            Some(&args.modules_root),
            args.source_root.as_deref(),
            roots,
            crate::GraphCompatibilityScope::ExactSourceUnit,
        )?;
    }

    let config = args
        .tree_config
        .as_deref()
        .context("--graphs requires --tree-config")?;
    let scope = scope_graphs_to_tree(&graphs, config)?;
    let mut contexts = crate::VerifiedGraphContexts::new();
    for (chunk_id, graph) in scope.configured {
        let context = crate::verify_graph_compatibility(
            &graph,
            &args.modules_root,
            args.source_root.as_deref(),
            roots,
            Some(config),
        )?;
        ensure!(
            context.chunk_id() == chunk_id,
            "configured graph chunk identity changed while verifying {}",
            graph.display()
        );
        contexts.insert(chunk_id, context);
    }
    Ok(contexts)
}

#[derive(Debug, Subcommand)]
enum ModulesNsCommand {
    /// Read, set, edit, or clear a module's top-level `comment:` field.
    ///
    /// Same modes as `bindings comment` (positional text / `--edit` /
    /// `--clear`; bare invocation reads). A module-level comment emits
    /// at the top of the generated module file and protects the module
    /// from auto-delete when `bindings assign` drains its members.
    Comment(ModuleCommentArgs),
    /// Splice source module YAMLs into a target YAML and delete the sources.
    ///
    /// Concatenates `members:`, `source_matches:`, `annotations:`, and
    /// `anonymous_statements:` into the target (created if needed);
    /// source `comment:` fields concatenate into the target's with a
    /// `--- from <source>:` divider, and `merged from: <sources>`
    /// provenance lands in the target's `note:`. Validates the
    /// post-merge assignment with the realizability gate (requires
    /// `--graph` unless `--no-verify`). `--dry-run --analysis` additionally
    /// reports statement-order commutation for the exact merged candidate; it
    /// requires the full tree transform context and does not change the gate.
    Merge(Box<MergeArgs>),
    /// Delete one or more module YAML files. Refuses non-empty modules unless `--force`.
    ///
    /// "Non-empty" means any `members:`, `source_matches:`,
    /// `annotations:`, or `anonymous_statements:`. Multiple paths delete
    /// atomically: every path is validated up front; if any check fails,
    /// nothing is deleted. Every deletion checks the final owner universe
    /// and import priority, including empty owners (requires `--graph`
    /// unless `--no-verify`). `--force` allows claims to return to residual.
    /// `--dry-run --analysis` additionally reports statement-order
    /// commutation for the exact post-delete candidate. It requires the full
    /// tree transform context and does not change the legacy gate.
    Delete(Box<DeleteArgs>),
    /// Construct an entry-residual peel into one fresh owner appended to the
    /// supplied priority, preserving existing assignments and their DFS trace.
    /// Returns a snapshot-bound plan or a scoped outcome. Check it with
    /// `modules check --plan` and preview with `modules apply --plan --dry-run`
    /// before applying it.
    Propose(ProposeArgs),
    /// Check an explicit source-unit destination batch against the complete
    /// accepted assignment. Omitted units retain their owners; writes no spec.
    Check(ModulesCheckArgs),
    /// Apply exact source-unit assignments.
    /// `--assignments` accepts the same changes as `modules check --batch`,
    /// including anonymous and existing units, and proves the entire resolved map.
    /// `--dry-run --analysis` additionally reports statement-order commutation for
    /// the exact candidate using the full tree transform context; it does not
    /// change the realizability gate.
    Apply(ModulesApplyArgs),
    /// List all modules in the spec with summary stats.
    ///
    /// Per-row: `member_count`, `anonymous_statement_count`, `residual`
    /// flag, `has_comment`.
    List(ModulesListArgs),
}

#[derive(Debug, ClapArgs)]
struct ModulesCheckArgs {
    #[command(flatten)]
    common: CommonArgs,
    /// JSON array of {unit_id, module} destination changes; `-` reads stdin.
    #[arg(long, required_unless_present = "plan", conflicts_with = "plan")]
    batch: Option<String>,
    /// Saved exact plan, including graph/program identity and logical destinations.
    #[arg(long, conflicts_with = "batch")]
    plan: Option<PathBuf>,
    /// Complete JSON array of nonentry module paths for an explicit batch that
    /// changes the owner universe or global import priority.
    #[arg(long = "import-order", conflicts_with = "plan")]
    import_order: Option<PathBuf>,
    /// Check graph constraints only; never synthesize selectors or return an
    /// executable plan. The default also preflights the source writer.
    #[arg(long, conflicts_with = "plan")]
    graph_only: bool,
    #[arg(long = "tree-config", env = "DEBUNDLE_TREE_CONFIG")]
    tree_config: Option<PathBuf>,
    #[arg(
        long = "source-root",
        env = "DEBUNDLE_SOURCE_ROOT",
        required_unless_present = "graph_only"
    )]
    source_root: Option<PathBuf>,
    #[arg(long, value_enum)]
    format: Option<OutputFormat>,
}

#[derive(Clone, Copy, serde::Serialize, strum::Display)]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
enum AssignmentCheckScope {
    GraphOnly,
    ExecutablePlan,
}

fn run_modules_check(
    args: ModulesCheckArgs,
    output: &mut dyn std::io::Write,
    stderr: &mut (dyn std::io::Write + Send),
    source_context: Option<&VerifiedGraphSourceContext>,
) -> Result<()> {
    let input = analysis::DependencyGraphInput::read(&args.common.dependency_graph_path)?;
    let (mut changes, import_order) = read_assignment_request(
        &args.common.dependency_graph_path,
        args.plan.as_deref(),
        args.batch.as_deref(),
        args.import_order.as_deref(),
    )?;
    if args.plan.is_none()
        && input.report().ownership_subset.is_none()
        && let Some(config) = &args.tree_config
    {
        let scope = spec_tree::ModuleTreeScope::from_config(config, &input.report().chunk_id)?;
        for change in &mut changes {
            let physical = scope.canonical_destination(&change.module)?;
            change.module = scope.logical_destination(&physical)?.to_owned();
        }
    }
    let problem = AssignmentProblem::prepare(&input)?;
    let import_order = import_order.unwrap_or_else(|| input.report().import_order.clone());
    let candidate = problem.candidate(&changes)?;
    let validation = problem.validate_with_priority(&candidate, &import_order)?;
    for change in &mut changes {
        let unit = input
            .source_unit_id(&change.unit_id)
            .expect("checked source unit");
        change.module = candidate[unit.0]
            .as_ref()
            .expect("explicit destination")
            .as_str()
            .to_string();
    }
    let graph_accepted = accepted(&validation);
    let selector_realization_error = if graph_accepted && !args.graph_only {
        let realization = (|| {
            let source_root = args
                .source_root
                .as_deref()
                .context("executable plan check requires --source-root for selector preflight")?;
            preflight_assignment(
                stderr,
                &input,
                &args.common.dependency_graph_path,
                &args.common.modules_root,
                source_root,
                source_context,
                preview_options(args.tree_config.as_deref(), changes.clone(), &import_order),
            )
        })();
        realization.err().map(|error| format!("{error:#}"))
    } else {
        None
    };
    let is_accepted = graph_accepted && selector_realization_error.is_none();
    #[derive(serde::Serialize)]
    struct CheckReport {
        scope: AssignmentCheckScope,
        accepted: bool,
        graph_accepted: bool,
        changes: Vec<SourceUnitAssignment>,
        import_order: Vec<spec::ModulePath>,
        residual_units_extracted: usize,
        existing_units_moved: usize,
        validation: gate::AssignmentReport,
        #[serde(skip_serializing_if = "Option::is_none")]
        selector_realization_error: Option<String>,
        plan: Option<AssignmentPlan>,
    }
    let report = CheckReport {
        scope: if args.graph_only {
            AssignmentCheckScope::GraphOnly
        } else {
            AssignmentCheckScope::ExecutablePlan
        },
        accepted: is_accepted,
        graph_accepted,
        plan: if is_accepted && !args.graph_only {
            Some(AssignmentPlan {
                snapshot: AssignmentSnapshot::read(&args.common.dependency_graph_path)?,
                changes: changes.clone(),
                import_order: import_order.clone(),
            })
        } else {
            None
        },
        changes,
        import_order,
        residual_units_extracted: problem.residual_gain(&candidate),
        existing_units_moved: problem.existing_units_moved(&candidate),
        validation: validation.report,
        selector_realization_error,
    };
    emit_report(
        output,
        args.format,
        &report,
        |report, text| {
            text.push_str(&format!(
                "scope={} accepted={} graph_accepted={} residual_units_extracted={} existing_units_moved={}\n",
                report.scope, report.accepted, report.graph_accepted, report.residual_units_extracted, report.existing_units_moved
            ));
            Ok(())
        },
        "writing assignment check",
    )?;
    if let Some(error) = &report.selector_realization_error {
        anyhow::bail!("selector realization rejected the batch: {error}");
    }
    ensure!(is_accepted, "complete assignment gate rejected the batch");
    Ok(())
}

fn run_assignment_search(
    args: &ProposeArgs,
    stderr: &mut (dyn std::io::Write + Send),
    source_contexts: &crate::VerifiedGraphContexts,
    output: &mut dyn std::io::Write,
) -> Result<()> {
    if let Some(reports_root) = &args.graphs_root {
        return run_graph_corpus_search(args, reports_root, stderr, source_contexts, output);
    }

    let graph_path = args
        .dependency_graph_path
        .as_deref()
        .context("one of --graph or --graphs is required")?;
    let (_, report, plan) =
        search_one_graph(args, graph_path, stderr, source_contexts.values().next())?;
    #[derive(serde::Serialize)]
    struct CompleteReport<'a> {
        #[serde(flatten)]
        search: &'a peel::search::SearchReport,
        plan: Option<AssignmentPlan>,
    }
    emit_report(
        output,
        args.format,
        &CompleteReport {
            search: &report,
            plan,
        },
        |report, text| {
            text.push_str(&serde_json::to_string_pretty(report)?);
            text.push('\n');
            Ok(())
        },
        "writing complete search",
    )?;
    Ok(())
}

struct ConstructedProposal {
    chunk_id: String,
    graph: PathBuf,
    input: Option<analysis::DependencyGraphInput>,
    search: peel::search::SearchReport,
}

fn construct_one_graph(
    args: &ProposeArgs,
    graph_path: &std::path::Path,
) -> Result<ConstructedProposal> {
    let input = analysis::DependencyGraphInput::read(graph_path)?;
    let chunk_id = input.report().chunk_id.clone();
    let problem = AssignmentProblem::new(&input)?;
    let into = args
        .into
        .iter()
        .map(|destination| spec::ModulePath::parse(destination, "").map(|path| path.to_string()))
        .collect::<std::result::Result<Vec<_>, _>>()?;
    let options = peel::search::SearchOptions {
        into,
        required: args.required.clone(),
        only_units: args
            .only_units
            .as_ref()
            .map(|path| -> Result<Vec<String>> { Ok(serde_json::from_slice(&fs::read(path)?)?) })
            .transpose()?,
    };
    let search = peel::search::search(&problem, &options)?;
    let input = (search.status == peel::search::SearchStatus::Found).then_some(input);
    Ok(ConstructedProposal {
        chunk_id,
        graph: graph_path.to_path_buf(),
        input,
        search,
    })
}

fn reject_selector_realization(report: &mut peel::search::SearchReport, error: &anyhow::Error) {
    report.status = peel::search::SearchStatus::InventoryOnly;
    report.blockers.push(format!(
        "selected closure cannot be realized by the source-unit writer: {error:#}"
    ));
}

fn preview_options<'a>(
    tree_config: Option<&'a Path>,
    assignments: Vec<SourceUnitAssignment>,
    import_order: &[spec::ModulePath],
) -> SourceUnitApplyOptions<'a> {
    SourceUnitApplyOptions {
        tree_config,
        assignments,
        import_order: Some(import_order.to_vec()),
        readable_names: Default::default(),
        selector_overrides: Default::default(),
        dry_run: true,
        effect_assessment_context: None,
    }
}

fn preflight_assignment(
    stderr: &mut (dyn std::io::Write + Send),
    input: &analysis::DependencyGraphInput,
    graph: &Path,
    modules_root: &Path,
    source_root: &Path,
    source_context: Option<&VerifiedGraphSourceContext>,
    mut preview: SourceUnitApplyOptions<'_>,
) -> Result<()> {
    preview.assignments = physical_plan_changes(
        preview.assignments,
        input,
        preview.tree_config,
        modules_root,
        source_context,
    )?;
    run_source_unit_apply(
        stderr,
        modules_root,
        graph,
        source_root,
        source_context,
        preview,
    )?;
    Ok(())
}

fn preflight_one_proposal(
    args: &ProposeArgs,
    proposal: &mut ConstructedProposal,
    stderr: &mut (dyn std::io::Write + Send),
    context: Option<&VerifiedGraphSourceContext>,
) {
    if proposal.search.status != peel::search::SearchStatus::Found {
        return;
    }
    let Some(source_root) = args.source_root.as_deref() else {
        proposal.search.status = peel::search::SearchStatus::InventoryOnly;
        proposal.search.blockers.push(
            "provide --source-root to preflight selector synthesis before receiving an executable plan".into(),
        );
        proposal.input = None;
        return;
    };
    let context = context.filter(|context| context.chunk_id() == proposal.chunk_id);
    let modules_root = args
        .tree_config
        .as_ref()
        .and(context)
        .and_then(VerifiedGraphSourceContext::modules_root)
        .unwrap_or(&args.modules_root);
    if let Err(error) = preflight_assignment(
        stderr,
        proposal
            .input
            .as_ref()
            .expect("found proposal retains its graph"),
        &proposal.graph,
        modules_root,
        source_root,
        context,
        preview_options(
            args.tree_config.as_deref(),
            proposal.search.changes.clone(),
            &proposal.search.import_order,
        ),
    ) {
        reject_selector_realization(&mut proposal.search, &error);
    }
    proposal.input = None;
}

fn take_proposal_plan(proposal: &mut ConstructedProposal) -> Result<Option<AssignmentPlan>> {
    if proposal.search.status != peel::search::SearchStatus::Found {
        return Ok(None);
    }
    Ok(Some(AssignmentPlan {
        snapshot: AssignmentSnapshot::read(&proposal.graph)?,
        changes: std::mem::take(&mut proposal.search.changes),
        import_order: std::mem::take(&mut proposal.search.import_order),
    }))
}

fn search_one_graph(
    args: &ProposeArgs,
    graph_path: &std::path::Path,
    stderr: &mut (dyn std::io::Write + Send),
    context: Option<&VerifiedGraphSourceContext>,
) -> Result<(String, peel::search::SearchReport, Option<AssignmentPlan>)> {
    let mut proposal = construct_one_graph(args, graph_path)?;
    preflight_one_proposal(args, &mut proposal, stderr, context);
    let plan = take_proposal_plan(&mut proposal)?;
    Ok((proposal.chunk_id, proposal.search, plan))
}

/// Preflight every selected row from one exact capture in one scoped source /
/// full-base frontend session. A failure remains graph-feasible inventory.
fn preflight_selected_corpus(
    args: &ProposeArgs,
    proposals: &mut [ConstructedProposal],
    contexts: &crate::VerifiedGraphContexts,
    stderr: &mut (dyn std::io::Write + Send),
) -> Result<usize> {
    struct Group {
        provenance: graph_provenance::GraphProvenance,
        rows: Vec<usize>,
    }
    let mut groups =
        std::collections::BTreeMap::<pipeline::SelectedOwnershipProgramKey, Group>::new();
    for (row, proposal) in proposals.iter_mut().enumerate() {
        let context = contexts
            .get(&proposal.chunk_id)
            .context("missing verified graph context")?;
        if proposal.search.status != peel::search::SearchStatus::Found {
            continue;
        }
        if args.source_root.is_none()
            || proposal
                .input
                .as_ref()
                .expect("found proposal retains its graph")
                .report()
                .ownership_subset
                .is_none()
        {
            preflight_one_proposal(args, proposal, stderr, Some(context));
            continue;
        }
        let header = graph_provenance::read_graph_header(&proposal.graph)?;
        ensure!(
            header.chunk_id == proposal.chunk_id,
            "graph chunk changed during proposal preparation"
        );
        let provenance = header
            .provenance
            .context("selected-view editing requires graph provenance")?;
        let key = pipeline::SelectedOwnershipProgram::key(context, &provenance)?;
        match groups.entry(key) {
            std::collections::btree_map::Entry::Vacant(entry) => {
                entry.insert(Group {
                    provenance,
                    rows: vec![row],
                });
            }
            std::collections::btree_map::Entry::Occupied(mut entry) => {
                entry.get_mut().rows.push(row);
            }
        }
    }
    let mut preparations = 0;
    for group in groups.into_values() {
        let first = &proposals[group.rows[0]];
        let context = contexts.get(&first.chunk_id).expect("verified context");
        let targets = group
            .rows
            .iter()
            .map(|row| proposals[*row].chunk_id.clone())
            .collect();
        preparations += 1;
        let prepared = (|| {
            let request = crate::graph_transform_request(context)?;
            let program =
                pipeline::SelectedOwnershipProgram::prepare(&request, context, &group.provenance)?;
            command_diagnostics::run(stderr, |diagnostics| {
                program.with_preflight_session(targets, diagnostics, |session| {
                    for row in &group.rows {
                        let proposal = &mut proposals[*row];
                        let context = contexts.get(&proposal.chunk_id).expect("verified context");
                        let realized = (|| {
                            program.verify_context(context, &group.provenance)?;
                            // Selected plans and their view keys are chunk-logical.
                            crate::selected_view_apply::run_prepared(
                                context,
                                proposal
                                    .input
                                    .take()
                                    .expect("each selected proposal consumes its graph once"),
                                preview_options(
                                    args.tree_config.as_deref(),
                                    proposal.search.changes.clone(),
                                    &proposal.search.import_order,
                                ),
                                session,
                            )
                        })();
                        if let Err(error) = realized {
                            reject_selector_realization(&mut proposal.search, &error);
                        }
                    }
                    Ok(())
                })
            })
        })();
        if let Err(error) = prepared {
            for row in &group.rows {
                reject_selector_realization(&mut proposals[*row].search, &error);
                proposals[*row].input = None;
            }
        }
    }
    Ok(preparations)
}

fn run_graph_corpus_search(
    args: &ProposeArgs,
    reports_root: &std::path::Path,
    stderr: &mut (dyn std::io::Write + Send),
    source_contexts: &crate::VerifiedGraphContexts,
    output: &mut dyn std::io::Write,
) -> Result<()> {
    use serde::Serialize;

    #[derive(Clone, Copy, Serialize)]
    #[serde(rename_all = "snake_case")]
    enum CorpusStatus {
        Found,
        InventoryOnly,
        UnsupportedResidualRouting,
        ExhaustedNoPeel,
    }

    #[derive(Serialize)]
    struct ChunkResult {
        chunk_id: String,
        graph: PathBuf,
        search: peel::search::SearchReport,
        plan: Option<AssignmentPlan>,
    }

    #[derive(Serialize)]
    struct CorpusScope {
        evaluated: &'static str,
        configured_chunks: Vec<String>,
        skipped_unconfigured_graphs: Vec<crate::module_proposals::UnconfiguredGraph>,
    }

    #[derive(Serialize)]
    struct PreparationWork {
        /// Number of selected source/full-base preparation attempts, including
        /// failed attempts. Each attempt serves one exact verified capture.
        selected_programs: usize,
    }

    #[derive(Serialize)]
    struct CorpusReport {
        status: CorpusStatus,
        preparation: PreparationWork,
        scope: CorpusScope,
        chunks: Vec<ChunkResult>,
    }

    let graphs = discover_graphs(reports_root)?;
    let config = args
        .tree_config
        .as_deref()
        .context("--graphs requires --tree-config")?;
    let GraphCorpusScope {
        configured,
        skipped_unconfigured,
    } = scope_graphs_to_tree(&graphs, config)?;

    let mut proposals = Vec::with_capacity(configured.len());
    for (chunk_id, graph) in configured {
        let proposal = construct_one_graph(args, &graph)?;
        ensure!(
            proposal.search.scope.required_units.is_empty(),
            "multi-graph proposal cannot carry required source-unit IDs"
        );
        ensure!(
            proposal.chunk_id == chunk_id,
            "graph header chunk ID changed while reading {}",
            graph.display()
        );
        proposals.push(proposal);
    }
    let selected_programs =
        preflight_selected_corpus(args, &mut proposals, source_contexts, stderr)?;
    let mut chunks = Vec::with_capacity(proposals.len());
    for mut proposal in proposals {
        let plan = take_proposal_plan(&mut proposal)?;
        chunks.push(ChunkResult {
            chunk_id: proposal.chunk_id,
            graph: proposal.graph,
            search: proposal.search,
            plan,
        });
    }

    let status = if chunks
        .iter()
        .any(|chunk| chunk.search.status == peel::search::SearchStatus::Found)
    {
        CorpusStatus::Found
    } else if chunks
        .iter()
        .any(|chunk| chunk.search.status == peel::search::SearchStatus::InventoryOnly)
    {
        CorpusStatus::InventoryOnly
    } else if chunks
        .iter()
        .any(|chunk| chunk.search.status == peel::search::SearchStatus::UnsupportedResidualRouting)
    {
        CorpusStatus::UnsupportedResidualRouting
    } else {
        CorpusStatus::ExhaustedNoPeel
    };
    let configured_chunks = chunks.iter().map(|chunk| chunk.chunk_id.clone()).collect();
    let report = CorpusReport {
        status,
        preparation: PreparationWork { selected_programs },
        scope: CorpusScope {
            evaluated: "configured_chunks_only",
            configured_chunks,
            skipped_unconfigured_graphs: skipped_unconfigured,
        },
        chunks,
    };
    emit_report(
        output,
        args.format,
        &report,
        |report, text| {
            text.push_str(&serde_json::to_string_pretty(report)?);
            text.push('\n');
            Ok(())
        },
        "writing graph corpus search",
    )?;
    Ok(())
}

/// Args for `debundle modules list`.
#[derive(Debug, ClapArgs)]
struct ModulesListArgs {
    /// Modules tree root.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    pub modules_root: PathBuf,

    /// Restrict to truly empty modules — no `members:`,
    /// `source_matches:`, `annotations:`, or `anonymous_statements:` —
    /// matching `modules delete`'s default-deletable predicate. A
    /// module with only `source_matches:`, `annotations:`, or
    /// `anonymous_statements:` is not empty and isn't reported.
    #[arg(long)]
    pub empty: bool,

    /// Restrict to authored modules in the `residual/` path namespace.
    #[arg(long)]
    /// Whether the authored path belongs to the `residual/` namespace.
    /// Independent of a generated graph destination's policy or entry role.
    pub residual: bool,

    /// Same predicate as `--empty`.
    #[arg(long = "unassigned-bindings")]
    pub unassigned_bindings: bool,

    /// Restrict to auto-deletable modules: truly empty (the `--empty`
    /// predicate) AND no module-level `comment:` or `note:`. This is the subset
    /// safe to sweep with `modules delete` — authored metadata would otherwise
    /// pin the module as a kept shell.
    #[arg(long = "auto-deletable")]
    pub auto_deletable: bool,

    /// Include each module's `anonymous_statement_count` in text output,
    /// alongside `member_count` (JSON always carries it). Surfaces the
    /// default entry's anonymous side-effect count over time.
    #[arg(long = "with-anonymous")]
    pub with_anonymous: bool,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

#[derive(Debug, Clone, serde::Serialize)]
struct ModuleListEntry {
    pub path: String,
    pub member_count: usize,
    /// Count of `anonymous_statements:` entries — side-effecting
    /// statements the module claims but that declare no binding.
    /// A module with `member_count == 0` and
    /// `anonymous_statement_count > 0` carries side effects on
    /// every rebuild even though it owns no named bindings.
    pub anonymous_statement_count: usize,
    pub residual: bool,
    pub has_comment: bool,
}

#[derive(Debug, Clone, serde::Serialize)]
struct ModulesListReport {
    pub modules: Vec<ModuleListEntry>,
}

fn run_modules_list(args: ModulesListArgs, output: &mut dyn std::io::Write) -> Result<()> {
    use spec::is_residual_namespace_path;
    use spec_modules::{collect_module_files, module_path_from_file, read_module_file};
    let files = collect_module_files(&args.modules_root)
        .with_context(|| format!("walking {}", args.modules_root.display()))?;
    let mut entries: Vec<ModuleListEntry> = Vec::new();
    for file in files {
        let module = read_module_file(&file)?;
        let path = module_path_from_file(&file, &args.modules_root);
        let residual = is_residual_namespace_path(&path);
        let source_match_binding_count = module
            .source_matches
            .iter()
            .map(|claim| claim.bindings.len())
            .sum::<usize>();
        let member_count = module.members.len() + source_match_binding_count;
        let entry = ModuleListEntry {
            path,
            member_count,
            anonymous_statement_count: module.anonymous_statements.len(),
            residual,
            has_comment: module.comment.is_some(),
        };
        let is_truly_empty = module.is_structurally_empty();
        let is_auto_deletable = module.is_auto_deletable();
        let keep = (!args.empty || is_truly_empty)
            && (!args.residual || entry.residual)
            && (!args.unassigned_bindings || is_truly_empty)
            && (!args.auto_deletable || is_auto_deletable);
        if keep {
            entries.push(entry);
        }
    }
    entries.sort_by(|a, b| a.path.cmp(&b.path));
    let report = ModulesListReport { modules: entries };
    let with_anonymous = args.with_anonymous;
    emit_report(
        output,
        args.format,
        &report,
        |report, out| {
            render_modules_list_text(report, out, with_anonymous);
            Ok(())
        },
        "writing modules list output",
    )
}

fn render_modules_list_text(report: &ModulesListReport, out: &mut String, with_anonymous: bool) {
    out.push_str(&format!("{} module(s)\n", report.modules.len()));
    for entry in &report.modules {
        let flags = match (entry.residual, entry.has_comment) {
            (true, true) => "[residual,doc]",
            (true, false) => "[residual]",
            (false, true) => "[doc]",
            (false, false) => "",
        };
        let anon = if with_anonymous {
            format!("  anon={}", entry.anonymous_statement_count)
        } else {
            String::new()
        };
        out.push_str(&format!(
            "  {}  members={}{}  {}\n",
            entry.path, entry.member_count, anon, flags
        ));
    }
}

//
// Each query command needs a text rendering for tty output. v1 keeps
// these compact: one line per item plus a brief summary header.

pub(super) fn run(
    stderr: &mut (dyn std::io::Write + Send),
    args: ModulesNs,
    source_contexts: &crate::VerifiedGraphContexts,
    analysis_engine_sha256: &'static str,
    output: &mut dyn std::io::Write,
) -> Result<()> {
    let source_context = source_contexts.values().next();
    match args.command {
        ModulesNsCommand::Comment(c) => run_module_comment_cmd(output, c),
        ModulesNsCommand::Merge(m) => {
            run_merge(output, stderr, *m, source_context, analysis_engine_sha256)
        }
        ModulesNsCommand::Delete(d) => {
            run_delete(output, stderr, *d, source_context, analysis_engine_sha256)
        }
        ModulesNsCommand::Propose(p) => run_assignment_search(&p, stderr, source_contexts, output),
        ModulesNsCommand::Check(args) => run_modules_check(args, output, stderr, source_context),
        ModulesNsCommand::Apply(args) => {
            run_modules_apply(output, stderr, args, source_context, analysis_engine_sha256)
        }
        ModulesNsCommand::List(args) => run_modules_list(args, output),
    }
}
