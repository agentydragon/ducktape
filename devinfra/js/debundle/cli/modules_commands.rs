//! `modules` command arguments, dispatch, and report presentation. Merge and
//! delete planning/persistence live in `module`, separate from CLI rendering.
use crate::binding::{SourceUnitAssignment, run_source_unit_refine};
use crate::comment::{ModuleCommentArgs, run_module_comment_cmd};
use crate::emit_report;
use crate::module::{DeleteArgs, MergeArgs, run_delete, run_merge};
use crate::module_apply::{ModulesApplyArgs, run_modules_apply};
use crate::outcome::{emit_gate_rejection_json, print_outcome_json};
use anyhow::{Context, Result};
use clap::{Args as ClapArgs, Subcommand};
use graph_provenance::VerifiedGraphSourceContext;
use peel::propose::{
    DEFAULT_SIZE_CAP_LINES, ModuleDecompositionCoarseningMode, ModuleDecompositionOptions,
    ModuleDecompositionReport, ModuleDecompositionStrategy, ProposalOptions,
    decompose_modules_from_files_with_options,
};
use peel::{CommonArgs, OutputFormat, PlanWorkArgs, run_plan_work_report};
use std::path::PathBuf;
use std::{fs, io::Read};

/// Args for `debundle modules ...`. Aggregates the existing
/// comment-edit verb (in `cli::comment`) with the new
/// `merge` / `delete` / `propose` verbs lifted from `module merge`
/// and the proposal planner.
#[derive(Debug, ClapArgs)]
pub(super) struct ModulesNs {
    #[command(subcommand)]
    command: ModulesNsCommand,
}

impl ModulesNs {
    pub(super) fn writes_with_graph(&self) -> bool {
        match &self.command {
            ModulesNsCommand::Merge(args) => {
                args.edit.dependency_graph_path.is_some() && !args.edit.dry_run
            }
            ModulesNsCommand::Delete(args) => {
                args.edit.dependency_graph_path.is_some() && !args.edit.dry_run
            }
            ModulesNsCommand::Apply(args) => !args.dry_run,
            ModulesNsCommand::Refine(args) => !args.dry_run,
            _ => false,
        }
    }
    pub(super) fn format(&self) -> Option<OutputFormat> {
        match &self.command {
            ModulesNsCommand::Merge(args) => args.edit.format,
            ModulesNsCommand::Delete(args) => args.edit.format,
            ModulesNsCommand::Propose(args) => args.format,
            ModulesNsCommand::Apply(args) => args.format,
            ModulesNsCommand::Refine(args) => args.format,
            ModulesNsCommand::Decompose(args) => args.format,
            _ => None,
        }
    }
    pub(super) fn verify_graph(
        &self,
        spec_root: Option<&std::path::Path>,
        graph_source_root: Option<&std::path::Path>,
        graph_package_roots: &std::collections::BTreeMap<String, PathBuf>,
    ) -> Result<Option<VerifiedGraphSourceContext>> {
        match &self.command {
            ModulesNsCommand::Merge(args) => verify_edit(
                &args.edit,
                spec_root,
                graph_source_root,
                graph_package_roots,
            ),
            ModulesNsCommand::Delete(args) => verify_edit(
                &args.edit,
                spec_root,
                graph_source_root,
                graph_package_roots,
            ),
            ModulesNsCommand::Propose(args) => crate::verify_graph_compatibility(
                &args.common.dependency_graph_path,
                &args.common.modules_root,
                args.source_root.as_deref(),
                spec_root,
                graph_source_root,
                graph_package_roots,
                args.tree_config.as_deref(),
            )
            .map(Some),
            ModulesNsCommand::Apply(args) => crate::verify_graph_compatibility(
                &args.graph,
                &args.modules_root,
                Some(&args.source_root),
                spec_root,
                graph_source_root,
                graph_package_roots,
                args.tree_config.as_deref(),
            )
            .map(Some),
            ModulesNsCommand::Refine(args) => crate::verify_graph_compatibility(
                &args.graph,
                &args.modules_root,
                Some(&args.source_root),
                spec_root,
                graph_source_root,
                graph_package_roots,
                args.tree_config.as_deref(),
            )
            .map(Some),
            ModulesNsCommand::Decompose(args) => crate::verify_graph_compatibility(
                &args.common.dependency_graph_path,
                &args.common.modules_root,
                args.source_root.as_deref(),
                spec_root,
                graph_source_root,
                graph_package_roots,
                args.tree_config.as_deref(),
            )
            .map(Some),
            _ => Ok(None),
        }
    }
}

fn verify_edit(
    edit: &crate::edit_gate::GraphEditArgs,
    spec_root: Option<&std::path::Path>,
    graph_source_root: Option<&std::path::Path>,
    graph_package_roots: &std::collections::BTreeMap<String, PathBuf>,
) -> Result<Option<VerifiedGraphSourceContext>> {
    if let Some(graph) = &edit.dependency_graph_path {
        return crate::verify_graph_compatibility(
            graph,
            &edit.modules_root,
            edit.source_root.as_deref(),
            spec_root,
            graph_source_root,
            graph_package_roots,
            None,
        )
        .map(Some);
    }
    Ok(None)
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
    /// `--graph` unless `--no-verify`).
    Merge(MergeArgs),
    /// Delete one or more module YAML files. Refuses non-empty modules unless `--force`.
    ///
    /// "Non-empty" means any `members:`, `source_matches:`,
    /// `annotations:`, or `anonymous_statements:`. Multiple paths delete
    /// atomically: every path is validated up front; if any check fails,
    /// nothing is deleted. All-empty deletions cannot change the
    /// assignment, so the gate is a no-op; `--force` deletions of
    /// non-empty modules run the realizability gate against the
    /// post-delete spec (requires `--graph` unless `--no-verify`).
    Delete(DeleteArgs),
    /// Emit module-assignment proposals derived from the atomic DAG.
    ///
    /// Read-only: surfaces *suggested* binding → module assignments +
    /// diagnostics; reviewed fresh residual proposals are claimed with
    /// `modules apply` (see docs/cli.md). With `--source-root`, JSON rows
    /// carry `landable_today`, `unaddressable_anonymous_source_unit_ids`, and
    /// `landability_notes` from full-JS-AST selector uniqueness.
    /// Rows also carry bounded `observed_consumers` evidence: incoming value
    /// reads, writes, and scheduling/promoted edges to added source units, with
    /// source units, current modules, raw edges, and total/omitted counts.
    /// Consumers already in the resulting proposal class are excluded. This
    /// evidence does not infer semantic ownership.
    /// `landable_today` is `false` both for proposals with unaddressable
    /// anonymous statements and for `blocked_residual_dependency`
    /// proposals (outgoing constraining edges into other residual classes)
    /// — the latter need their closure grown or manual co-location
    /// before they can land.
    Propose(PlanWorkArgs),
    /// Claim reviewed residual proposal bindings using proven structural selectors.
    Apply(ModulesApplyArgs),
    /// Move reviewed existing source-unit claims as one validated batch.
    ///
    /// The JSON batch contains `{ "unit_id": "...", "module": "..." }`
    /// rows. Omitted source units keep their current module. This command moves
    /// existing selectors and metadata without synthesizing selectors, then
    /// resolves the entire scoped spec again and requires the complete source-unit
    /// assignment to match the requested assignment before running the normal
    /// realizability and atomic-group gates. Import/export scaffolding and
    /// residual source units cannot be moved. Module comments and notes stay with
    /// their original YAML module.
    Refine(ModulesRefineArgs),
    /// Report a read-only, gate-checked refinement of existing module groups.
    ///
    /// The planner keeps each current module separate from the others and
    /// groups its source units by the gated atomic/same-module seed classes. It
    /// validates the complete candidate against the structural atoms and
    /// shared realizability gate. `--atomic` skips same-module seed contraction
    /// for a faster, more fragmented structural baseline. Suggested paths are
    /// review aids; the command does not edit the spec. Pair `--atomic` with
    /// `--include-residual` to split residual body atoms while keeping top-level
    /// imports and exports in the entry module. `--preserve-selector-anchors`
    /// resolves current claims using `--source-root` and keeps local
    /// intrinsic aliases and source_match member references together.
    /// `--include-unchanged` adds one-fragment rows for current named modules
    /// that the selected strategy leaves intact.
    Decompose(ModuleDecomposeArgs),
    /// List all modules in the spec with summary stats.
    ///
    /// Per-row: `member_count`, `anonymous_statement_count`, `residual`
    /// flag, `has_comment`.
    List(ModulesListArgs),
}

#[derive(Debug, ClapArgs)]
struct ModuleDecomposeArgs {
    #[command(flatten)]
    common: CommonArgs,

    /// Tree config for chunk-scoped module claims in a multi-chunk spec.
    #[arg(long = "tree-config")]
    tree_config: Option<PathBuf>,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    format: Option<OutputFormat>,

    /// Use structural atomic groups only, skipping same-module seed contractions.
    #[arg(long)]
    atomic: bool,

    /// With `--atomic`, also split residual body atoms into proposed modules.
    /// Top-level import and export declarations stay in the entry module.
    #[arg(long, requires = "atomic")]
    include_residual: bool,

    /// With `--atomic`, merge only module groups named by blocking SCCs from
    /// the shared gate, revalidating the complete candidate after each round.
    #[arg(long, requires = "atomic")]
    coarsen_blocking_sccs: bool,

    /// First co-locate whole candidate groups joined by reported structural
    /// atomic groups, then run the normal blocking-SCC coarsening.
    #[arg(long, requires = "coarsen_blocking_sccs")]
    coarsen_atomic_conflicts: bool,

    /// Rejoin accepted atomic fragments from the same original module when
    /// each incremental contraction still passes the shared gate.
    #[arg(long, requires = "coarsen_blocking_sccs")]
    rejoin_original_modules: bool,

    /// Limit speculative pair checks per original home across all rejoining
    /// passes. Whole-home checks remain available; zero skips pair fallback.
    #[arg(long, requires = "rejoin_original_modules")]
    rejoin_pair_attempt_limit: Option<usize>,

    /// Limit complete stable-order passes over original homes during rejoining.
    /// Zero skips rejoining; the final complete candidate is still gate-checked.
    #[arg(long, requires = "rejoin_original_modules")]
    rejoin_home_pass_limit: Option<usize>,

    /// Source root used to resolve existing source-backed and relational claims.
    #[arg(long = "source-root", env = "DEBUNDLE_SOURCE_ROOT")]
    source_root: Option<PathBuf>,

    /// Keep local intrinsic_alias members and source_match captures with their
    /// resolved in-module referenced members.
    #[arg(long, requires = "source_root")]
    preserve_selector_anchors: bool,

    /// Include unchanged current named modules as one-fragment report rows.
    #[arg(long)]
    include_unchanged: bool,
}

#[derive(Debug, ClapArgs)]
struct ModulesRefineArgs {
    /// JSON array of reviewed source-unit IDs and extensionless destination module IDs; `-` reads stdin.
    #[arg(long)]
    batch: String,

    /// Modules tree root.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    modules_root: PathBuf,

    /// Current dependency graph used to resolve selectors and check the complete result.
    #[arg(long = "graph", env = "DEBUNDLE_GRAPH")]
    graph: PathBuf,

    /// Root for the chunk source used by existing selectors.
    #[arg(long = "source-root", env = "DEBUNDLE_SOURCE_ROOT")]
    source_root: PathBuf,

    /// Tree config that scopes this graph-backed edit to its chunk's module roots.
    #[arg(long = "tree-config", env = "DEBUNDLE_TREE_CONFIG")]
    tree_config: Option<PathBuf>,

    /// Validate the complete edit without writing module YAML.
    #[arg(long)]
    dry_run: bool,

    /// Output format. Default text on tty, JSON on pipe.
    #[arg(long, value_enum)]
    format: Option<OutputFormat>,
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

fn run_modules_list(args: ModulesListArgs) -> Result<()> {
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
        args.format,
        &report,
        |report, out| render_modules_list_text(report, out, with_anonymous),
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

fn render_module_decomposition_text(report: &ModuleDecompositionReport, out: &mut String) {
    let strategy = match report.strategy {
        ModuleDecompositionStrategy::Seed => "seed",
        ModuleDecompositionStrategy::Atomic => "atomic",
        ModuleDecompositionStrategy::AtomicIncludingResidual => "atomic_including_residual",
    };
    out.push_str(&format!("strategy: {strategy}\n"));
    if report.preserve_selector_anchors {
        let relation_count = report
            .selector_anchor_relations
            .as_ref()
            .map_or(0, Vec::len);
        out.push_str(&format!(
            "selector anchor cohesion: enabled ({relation_count} local selector-anchor relation(s))\n"
        ));
    }
    out.push_str(if report.candidate_is_realizable {
        "candidate is realizable\n"
    } else {
        "candidate is not realizable\n"
    });
    out.push_str(&format!(
        "coverage: {}/{} source units; unchanged modules={}\n",
        report.assigned_source_unit_count, report.source_unit_count, report.unchanged_module_count
    ));
    for module in &report.modules {
        out.push_str(&format!(
            "{} → {} part(s)\n",
            module.module_id,
            module.fragments.len()
        ));
        for fragment in &module.fragments {
            out.push_str(&format!(
                "  {}  source_units={} bindings={}\n",
                fragment.suggested_module_path,
                fragment.source_unit_ids.join(", "),
                fragment.binding_ids.join(", "),
            ));
        }
    }
    for conflict in &report.atomic_group_conflicts {
        out.push_str(&format!(
            "atomic group {} spans candidate modules: {}\n",
            conflict.atomic_group_id,
            conflict
                .candidate_modules
                .iter()
                .map(ToString::to_string)
                .collect::<Vec<_>>()
                .join(", "),
        ));
    }
    if !report.cycles.is_empty() {
        out.push_str(&format!("gate cycles: {}\n", report.cycles.len()));
    }
    if !report.cross_rebinds.is_empty() {
        out.push_str(&format!(
            "cross-module rebinds: {}\n",
            report.cross_rebinds.len()
        ));
    }
    if let Some(coarsening) = &report.coarsening {
        out.push_str(&format!(
            "SCC coarsening stopped: {:?}; rounds={}\n",
            coarsening.stop_reason,
            coarsening.rounds.len()
        ));
        for merge in &coarsening.atomic_conflict_merges {
            out.push_str(&format!(
                "  atomic groups {}: merged {} into {}\n",
                merge.atomic_group_ids.join(", "),
                merge
                    .merged_candidate_paths
                    .iter()
                    .map(ToString::to_string)
                    .collect::<Vec<_>>()
                    .join(", "),
                merge.suggested_module_path,
            ));
        }
        for (round_idx, round) in coarsening.rounds.iter().enumerate() {
            out.push_str(&format!(
                "  round {}: merged {} blocking SCC(s) into {}\n",
                round_idx + 1,
                round.blocking_sccs.len(),
                round
                    .suggested_merge_paths
                    .iter()
                    .map(ToString::to_string)
                    .collect::<Vec<_>>()
                    .join(", "),
            ));
        }
        for group in &coarsening.groups {
            out.push_str(&format!(
                "  final {}  origins={} source_units={} bindings={}\n",
                group.suggested_module_path,
                group
                    .origin_module_paths
                    .iter()
                    .map(ToString::to_string)
                    .collect::<Vec<_>>()
                    .join(", "),
                group.source_unit_ids.join(", "),
                group.binding_ids.join(", "),
            ));
        }
        if let Some(collisions) = &coarsening.naming_collisions {
            for collision in collisions {
                out.push_str(&format!(
                    "  name collision {} in {}: {}\n",
                    collision.readable_name,
                    collision.suggested_module_path,
                    collision
                        .claims
                        .iter()
                        .map(|claim| format!(
                            "{}:{} (source units {})",
                            claim.original_module_path,
                            claim.binding_id,
                            claim.source_unit_ids.join(", ")
                        ))
                        .collect::<Vec<_>>()
                        .join("; ")
                ));
            }
        }
        if let Some(rejoining) = &coarsening.original_home_rejoining {
            let whole_home_pass_limit = rejoining
                .whole_home_pass_limit
                .map_or_else(|| "unlimited".to_owned(), |limit| limit.to_string());
            out.push_str(&format!(
                "original-home rejoining stopped: {:?}; restored={}/{} groups={} largest_group_source_units={} largest_group_origins={} contractions={}/{} whole_home_passes={}/{} budget_limited_homes={}\n",
                rejoining.stop_reason,
                rejoining.fully_restored_module_count,
                rejoining.original_module_count,
                rejoining.final_group_count,
                rejoining.largest_group_source_unit_count,
                rejoining.largest_group_origin_module_count,
                rejoining.accepted_contractions,
                rejoining.attempted_contractions,
                rejoining.whole_home_pass_count,
                whole_home_pass_limit,
                rejoining.budget_limited_home_count,
            ));
        }
    }
}

fn run_module_decompose(
    args: ModuleDecomposeArgs,
    source_context: Option<&VerifiedGraphSourceContext>,
) -> Result<()> {
    anyhow::ensure!(
        !args.include_residual || args.atomic,
        "--include-residual requires --atomic"
    );
    let report = decompose_modules_from_files_with_options(
        &ProposalOptions {
            dependency_graph_path: args.common.dependency_graph_path,
            modules_root: args.common.modules_root,
            tree_config: args.tree_config,
            source_root: args.source_root,
            source_context: source_context.cloned(),
            size_cap_lines: DEFAULT_SIZE_CAP_LINES,
        },
        ModuleDecompositionOptions {
            strategy: if args.atomic {
                if args.include_residual {
                    ModuleDecompositionStrategy::AtomicIncludingResidual
                } else {
                    ModuleDecompositionStrategy::Atomic
                }
            } else {
                ModuleDecompositionStrategy::Seed
            },
            coarsening: if args.coarsen_blocking_sccs {
                ModuleDecompositionCoarseningMode::MergeBlockingSccs
            } else {
                ModuleDecompositionCoarseningMode::None
            },
            preserve_selector_anchors: args.preserve_selector_anchors,
            include_unchanged: args.include_unchanged,
            coarsen_atomic_conflicts: args.coarsen_atomic_conflicts,
            rejoin_original_modules: args.rejoin_original_modules,
            rejoin_pair_attempt_limit: args.rejoin_pair_attempt_limit,
            whole_home_pass_limit: args.rejoin_home_pass_limit,
        },
    )?;
    emit_report(
        args.format,
        &report,
        render_module_decomposition_text,
        "writing module decomposition output",
    )
}

fn run_modules_refine(
    args: ModulesRefineArgs,
    source_context: Option<&VerifiedGraphSourceContext>,
) -> Result<()> {
    let text = if args.batch == "-" {
        let mut text = String::new();
        std::io::stdin().read_to_string(&mut text)?;
        text
    } else {
        fs::read_to_string(&args.batch).with_context(|| format!("reading {}", args.batch))?
    };
    let assignments: Vec<SourceUnitAssignment> =
        serde_json::from_str(&text).context("parsing modules refine batch")?;
    let outcome = match run_source_unit_refine(
        &args.modules_root,
        &args.graph,
        &args.source_root,
        source_context,
        args.tree_config.as_deref(),
        assignments,
        args.dry_run,
    ) {
        Ok(outcome) => outcome,
        Err(err) => {
            emit_gate_rejection_json("refine", args.format, &err);
            return Err(err);
        }
    };
    match OutputFormat::resolve(args.format) {
        OutputFormat::Text => println!(
            "{}: {} source_unit(s), {} file(s) written, {} file(s) deleted",
            outcome.outcome.action,
            outcome.source_units_reassigned,
            outcome.outcome.files_written.len(),
            outcome.outcome.files_deleted.len(),
        ),
        format => print_outcome_json(&outcome, format)?,
    }
    Ok(())
}

// --- Text renderers -------------------------------------------------
//
// Each query command needs a text rendering for tty output. v1 keeps
// these compact: one line per item plus a brief summary header.

fn render_plan_work_text(report: &peel::PlanWorkReport, out: &mut String) {
    out.push_str(&format!(
        "{} proposal(s), {} diagnostic(s)\n",
        report.report.proposals.len(),
        report.report.diagnostics.len(),
    ));
    for proposal in &report.report.proposals {
        let landable = if proposal.landable_today {
            "landable"
        } else {
            "needs-manual-work"
        };
        out.push_str(&format!(
            "  {}  source_units={} size={} {}\n",
            proposal.proposed_module_id,
            proposal.source_unit_ids.len(),
            proposal.size_lines_estimate,
            landable,
        ));
        for note in &proposal.landability_notes {
            out.push_str(&format!("    note: {note}\n"));
        }
        if !proposal.unaddressable_anonymous_source_unit_ids.is_empty() {
            out.push_str(&format!(
                "    unaddressable anonymous source units: {}\n",
                proposal.unaddressable_anonymous_source_unit_ids.join(", "),
            ));
        }
    }
}

pub(super) fn run(
    args: ModulesNs,
    source_context: Option<&VerifiedGraphSourceContext>,
) -> Result<()> {
    match args.command {
        ModulesNsCommand::Comment(c) => run_module_comment_cmd(c),
        ModulesNsCommand::Merge(m) => run_merge(m, source_context),
        ModulesNsCommand::Delete(d) => run_delete(d, source_context),
        ModulesNsCommand::Propose(p) => {
            let report = run_plan_work_report(&p, source_context)?;
            emit_report(
                p.format,
                &report,
                render_plan_work_text,
                "writing propose output",
            )
        }
        ModulesNsCommand::Decompose(args) => run_module_decompose(args, source_context),
        ModulesNsCommand::Apply(args) => run_modules_apply(args, source_context),
        ModulesNsCommand::Refine(args) => run_modules_refine(args, source_context),
        ModulesNsCommand::List(args) => run_modules_list(args),
    }
}
