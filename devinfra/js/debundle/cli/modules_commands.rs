//! `modules` command arguments, dispatch, and report presentation. Merge and
//! delete planning/persistence live in `module`, separate from CLI rendering.
use crate::comment::{ModuleCommentArgs, run_module_comment_cmd};
use crate::emit_report;
use crate::module::{DeleteArgs, MergeArgs, run_delete, run_merge};
use crate::module_apply::{ModulesApplyArgs, run_modules_apply};
use anyhow::{Context, Result};
use clap::{Args as ClapArgs, Subcommand};
use peel::propose::{
    DEFAULT_SIZE_CAP_LINES, ModuleDecompositionCoarseningMode, ModuleDecompositionOptions,
    ModuleDecompositionReport, ModuleDecompositionStrategy, ProposalOptions,
    decompose_modules_from_files_with_options,
};
use peel::{CommonArgs, OutputFormat, PlanWorkArgs, run_plan_work_report};
use std::path::PathBuf;

/// Args for `debundle modules ...`. Aggregates the existing
/// comment-edit verb (in `cli::comment`) with the new
/// `merge` / `delete` / `propose` verbs lifted from `module merge`
/// and the proposal planner.
#[derive(Debug, ClapArgs)]
pub(super) struct ModulesNs {
    #[command(subcommand)]
    command: ModulesNsCommand,
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
    /// post-merge partition with the realizability gate (requires
    /// `--graph` unless `--no-verify`).
    Merge(MergeArgs),
    /// Delete one or more module YAML files. Refuses non-empty modules unless `--force`.
    ///
    /// "Non-empty" means any `members:`, `source_matches:`,
    /// `annotations:`, or `anonymous_statements:`. Multiple paths delete
    /// atomically: every path is validated up front; if any check fails,
    /// nothing is deleted. All-empty deletions cannot change the
    /// partition, so the gate is a no-op; `--force` deletions of
    /// non-empty modules run the realizability gate against the
    /// post-delete spec (requires `--graph` unless `--no-verify`).
    Delete(DeleteArgs),
    /// Emit module-assignment proposals derived from the atomic DAG.
    ///
    /// Read-only: surfaces *suggested* binding → module assignments +
    /// diagnostics; reviewed fresh residual proposals are claimed with
    /// `modules apply` (see docs/cli.md). With `--source-root`, JSON rows
    /// carry `landable_today`, `unaddressable_anonymous_owner_ids`, and
    /// `landability_notes` from full-JS-AST selector uniqueness.
    /// `landable_today` is `false` both for proposals with unaddressable
    /// anonymous statements and for `blocked_residual_dependency`
    /// proposals (outgoing constraining edges into other residual cells)
    /// — the latter need their closure grown or manual co-location
    /// before they can land.
    Propose(PlanWorkArgs),
    /// Claim reviewed residual proposal bindings using proven structural selectors.
    Apply(ModulesApplyArgs),
    /// Report a read-only, gate-checked refinement of existing module groups.
    ///
    /// The planner keeps each current module separate from the others and
    /// groups its owners by the gated atomic/same-module seed classes. It
    /// validates the complete candidate against the structural atoms and
    /// shared realizability gate. `--atomic` skips same-module seed contraction
    /// for a faster, more fragmented structural baseline. Suggested paths are
    /// review aids; the command does not edit the spec. Pair `--atomic` with
    /// `--include-residual` to split residual body atoms while keeping top-level
    /// imports and exports in the entry module.
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

    /// Use structural atomic units only, skipping same-module seed contractions.
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

    /// Restrict to residual modules (any module whose path starts with `residual/`).
    #[arg(long)]
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
    /// residual sentinel's side-effect drift over time.
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
    use spec::is_residual_module_path;
    use spec_modules::{collect_module_files, module_path_from_file, read_module_file};
    let files = collect_module_files(&args.modules_root)
        .with_context(|| format!("walking {}", args.modules_root.display()))?;
    let mut entries: Vec<ModuleListEntry> = Vec::new();
    for file in files {
        let module = read_module_file(&file)?;
        let path = module_path_from_file(&file, &args.modules_root);
        let residual = is_residual_module_path(&path);
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
    out.push_str(if report.candidate_is_realizable {
        "candidate is realizable\n"
    } else {
        "candidate is not realizable\n"
    });
    out.push_str(&format!(
        "coverage: {}/{} owners; unchanged modules={}\n",
        report.assigned_owner_count, report.owner_count, report.unchanged_module_count
    ));
    for module in &report.modules {
        out.push_str(&format!(
            "{} → {} part(s)\n",
            module.module_id,
            module.fragments.len()
        ));
        for fragment in &module.fragments {
            out.push_str(&format!(
                "  {}  owners={} bindings={}\n",
                fragment.suggested_module_path,
                fragment.owner_ids.join(", "),
                fragment.binding_ids.join(", "),
            ));
        }
    }
    for conflict in &report.atomic_unit_conflicts {
        out.push_str(&format!(
            "atomic unit {} spans candidate modules: {}\n",
            conflict.unit_id,
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
                "  final {}  origins={} owners={} bindings={}\n",
                group.suggested_module_path,
                group
                    .origin_module_paths
                    .iter()
                    .map(ToString::to_string)
                    .collect::<Vec<_>>()
                    .join(", "),
                group.owner_ids.join(", "),
                group.binding_ids.join(", "),
            ));
        }
    }
}

fn run_module_decompose(args: ModuleDecomposeArgs) -> Result<()> {
    anyhow::ensure!(
        !args.include_residual || args.atomic,
        "--include-residual requires --atomic"
    );
    let report = decompose_modules_from_files_with_options(
        &ProposalOptions {
            owner_graph_path: args.common.owner_graph_path,
            modules_root: args.common.modules_root,
            tree_config: args.tree_config,
            source_root: None,
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
        },
    )?;
    emit_report(
        args.format,
        &report,
        render_module_decomposition_text,
        "writing module decomposition output",
    )
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
            "  {}  owners={} size={} {}\n",
            proposal.proposed_module_id,
            proposal.owner_ids.len(),
            proposal.size_lines_estimate,
            landable,
        ));
        for note in &proposal.landability_notes {
            out.push_str(&format!("    note: {note}\n"));
        }
        if !proposal.unaddressable_anonymous_owner_ids.is_empty() {
            out.push_str(&format!(
                "    unaddressable anonymous owners: {}\n",
                proposal.unaddressable_anonymous_owner_ids.join(", "),
            ));
        }
    }
}

pub(super) fn run(args: ModulesNs) -> Result<()> {
    match args.command {
        ModulesNsCommand::Comment(c) => run_module_comment_cmd(c),
        ModulesNsCommand::Merge(m) => run_merge(m),
        ModulesNsCommand::Delete(d) => run_delete(d),
        ModulesNsCommand::Propose(p) => {
            let report = run_plan_work_report(&p)?;
            emit_report(
                p.format,
                &report,
                render_plan_work_text,
                "writing propose output",
            )
        }
        ModulesNsCommand::Decompose(args) => run_module_decompose(args),
        ModulesNsCommand::Apply(args) => run_modules_apply(args),
        ModulesNsCommand::List(args) => run_modules_list(args),
    }
}
