pub mod binding;
pub mod comment;
pub mod edit_gate;
pub mod gate;
pub mod module;
pub mod outcome;
pub mod scc_cluster;
pub mod validate;
pub mod yaml_edit;

use std::path::PathBuf;

use crate::binding::{
    AssignOutcome, BindingsListFilters, Move, UnassignOutcome, parse_batch_json, parse_move_triple,
    rename_binding, run_bindings_assign, run_bindings_list, run_bindings_unassign,
};
use crate::comment::{
    BindingCommentArgs, ModuleCommentArgs, run_binding_comment_cmd, run_module_comment_cmd,
};
use crate::edit_gate::Gate;
use crate::gate::{GateArgs, run_gate_cli};
use crate::module::{DeleteArgs, MergeArgs, run_delete, run_merge};
use crate::outcome::{emit_gate_rejection_json, print_outcome_json};
use crate::scc_cluster::{ClusterArgs, SccArgs, run_cluster, run_scc};
use crate::validate::{ValidateArgs, run_validate_cmd};
use anyhow::{Context, Result, bail};
use clap::{Args as ClapArgs, Parser, Subcommand};
use peel::factorize::DEFAULT_SIZE_CAP_LINES;
use peel::{
    CommonArgs as PeelCommonArgs, ExplainArgs, GraphSummaryArgs, OutputFormat, PatchPlanArgs,
    PlanWorkArgs, SelectionArgs, SourceSliceArgs, UnitsArgs, print_report, run_explain_report,
    run_graph_summary_report, run_patch_plan_report, run_plan_work_report, run_source_slice_report,
    run_units_report,
};
use pipeline::{TransformArgs, TransformRunOptions, run_transform_cli_with_options};
use selector_codemod::match_selector::{
    MatchSelectorConfig, render_match_selector_text, run_match_selector,
};
use selector_codemod::{
    SelectorCodemodConfig, SelectorCodemodRewrite, render_selector_codemod_text,
    run_selector_codemod,
};
use selector_debt::{
    SelectorDebtReport, SourceAwareSelectorDebtConfig, compute_selector_debt_with_source,
    populate_name_only_module_groups, render_selector_debt_text,
};
use spec_modules::{collect_module_files, module_path_from_file};
use spec_stats::{SpecStats, compute_spec_stats, render_spec_stats_text};

/// Read and JSON-parse an `owner_graph.json` into an [`OwnerGraphReport`],
/// the load every `cli` verb that takes `--graph` shares.
pub(crate) fn load_owner_graph_report(
    path: &std::path::Path,
) -> Result<analysis::OwnerGraphReport> {
    let text =
        std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
    serde_json::from_str(&text).with_context(|| format!("parsing owner graph {}", path.display()))
}

#[derive(Debug, Parser)]
#[command(
    name = "debundle",
    version,
    about = "Debundle JavaScript bundles and inspect peelable module work.",
    long_about = "Runs the debundle transform pipeline and exposes JSON peel-planning queries over generated owner graphs and spec modules."
)]
pub struct DebundleArgs {
    #[command(subcommand)]
    command: DebundleCommand,
}

#[derive(Debug, Subcommand)]
enum DebundleCommand {
    /// Run the debundle transform pipeline from a flat or tree-shaped spec.
    ///
    /// Parse + facts + owner_graph + atomic_units + realizability gate +
    /// lower + emit. The gate is part of the pipeline contract: an
    /// unrealizable spec is rejected (there is no `run --no-verify` — fix
    /// the spec), and the rejection evidence (`cycles.json` /
    /// `atomic_unit_conflicts.json`) is written under
    /// `reports/tree/<chunk>/` next to `owner_graph.json`, where
    /// `debundle gate list` / `gate describe` read it.
    Run(TransformArgs),
    /// Per-binding spec verbs (list, assign, unassign, rename, comment).
    Bindings(BindingsNs),
    /// Module-level spec verbs (list, merge, delete, propose, comment).
    Modules(ModulesNs),
    /// List structural atoms (owner-level SCCs of the constraining-edge
    /// graph; per docs/design.md § "Two classes of atom").
    Atoms(UnitsArgs),
    /// Report spec coverage against atoms: which atoms are claimed,
    /// which fall through to residual.
    Coverage(PatchPlanArgs),
    /// High-level graph counts (owners, edges, atoms,
    /// residual-eligible bindings, …).
    #[command(name = "graph-summary")]
    GraphSummary(GraphSummaryArgs),
    /// Dereference any identifier (binding, module path, proposal,
    /// atom, owner, diagnostic) with full graph + spec context.
    Describe(DescribeArgs),
    /// Print the source text for any identifier.
    ///
    /// Same `<id>` dispatch as `debundle describe`; a module path,
    /// unambiguous module filename, or module id prints the
    /// concatenated source of every owner statement in the module, in
    /// declaration order.
    #[command(name = "show-source")]
    ShowSource(ShowSourceArgs),
    /// List SCCs in the module-quotient graph.
    Scc(SccArgs),
    /// List the module-quotient neighbors of a binding's owner.
    Cluster(ClusterArgs),
    /// Resolve an owner-graph EDB with the in-process Datalog solver and report
    /// name-pin categoricity + the derived alias count. With `--check`, exit
    /// non-zero unless name-pin resolution is total + categorical (the
    /// bootstrap-precondition shadow gate).
    #[command(name = "selector-solve")]
    SelectorSolve(SelectorSolveArgs),
    /// Spec-wide queries (e.g. `spec stats`).
    Spec(SpecNs),
    /// Query the realizability gate's rejected SCCs (list / describe / cut).
    Gate(GateArgs),
}

/// Args for `debundle selector-solve`.
#[derive(Debug, ClapArgs)]
pub struct SelectorSolveArgs {
    /// Owner-graph JSON (the `selector_solve` EDB).
    owner_graph: PathBuf,
    /// Exit non-zero unless name-pin resolution is total + categorical.
    #[arg(long)]
    check: bool,
}

/// Args for `debundle spec ...`.
#[derive(Debug, ClapArgs)]
pub struct SpecNs {
    #[command(subcommand)]
    command: SpecNsCommand,
}

#[derive(Debug, Subcommand)]
enum SpecNsCommand {
    /// Emit spec-wide summary: module + binding totals and member-count buckets.
    ///
    /// One pass over the modules tree. `modules` totals (`total`,
    /// `residual`, `empty`, `with_comment`) + `member_count` buckets
    /// (`min`, `max`, `singletons`, `tiny_2_to_5`, `medium_6_to_20`,
    /// `large_21_plus`), plus `bindings` totals (`total`, `renamed`,
    /// `unrenamed`, `orphan`, `with_comment`). Same per-row counters as
    /// `debundle modules list` / `debundle bindings list` (including the
    /// truly-empty definition of `empty`), folded into one report so
    /// callers don't `jq`.
    Stats(SpecStatsArgs),
    /// Rank the spec's rebuild-fragile selectors.
    ///
    /// Sections: **name-only** selectors (`selector.binding`) scored by
    /// how minified the bound name looks (1-2 chars / vowel-less rank
    /// highest), most fragile first; **name-only module groups** when
    /// `--group-module-depth` is set; **repeated source_match** bodies
    /// copied verbatim (whitespace-insensitive) across source-backed
    /// binding claims and anonymous statements — the copies a contextual
    /// selector should replace; and, with `--against`, **drifted
    /// bindings** — members whose readable `name:` is stable but whose
    /// `selector.binding.name` changed between the two spec versions.
    ///
    /// Opt in to source-aware checks with `--source-file` or
    /// `--source-root --chunk`: adds **near-ambiguous** rows (structural
    /// selectors that match exactly one top-level statement today but
    /// have high-scoring non-matching siblings), **repeated exact** rows
    /// (copied structural selector bodies resolving to the same body
    /// indices), and **grouped source-match suggestions** (repeated
    /// claims resolving to one multi-declarator `var`/`let`/`const`
    /// declaration, collapsible into one `source_matches[]` entry with
    /// `DECLARATORS_*` holes). Without source flags it reads only the
    /// modules tree.
    #[command(name = "selector-debt")]
    SelectorDebt(SelectorDebtArgs),
    /// Dry-run or apply mechanical selector rewrites across module YAML.
    ///
    /// Dry-run by default; `--apply` writes. Scope the run with
    /// `--file`, `--module`, `--module-prefix`, or `--item`. Skipped
    /// rows carry a machine-readable reason.
    #[command(name = "selector-codemod")]
    SelectorCodemod(SelectorCodemodArgs),
    /// Synthesize structural selectors for selected name-only members.
    ///
    /// Alias for `selector-codemod --rewrite
    /// name-binding-to-source-match`: given module exports, produce a
    /// forward-compatible `source_matches[]` selector that uniquely
    /// selects them, proven with the production matcher. Prefers the
    /// loosest readable unique form — holes and stable anchors over long
    /// exact current-source bodies, which can be over-narrow even when
    /// they match today. Automated forms: exact function/class
    /// declarations, and `var`/`let`/`const` declaration groups with
    /// non-target declarator runs collapsed to `DECLARATORS_BEFORE` /
    /// `DECLARATORS_BETWEEN` / `DECLARATORS_AFTER`. Dry-run JSON
    /// includes candidate count, matched top-level body index, group id,
    /// rewritten holes, and skip reasons.
    #[command(name = "synthesize-selectors")]
    SynthesizeSelectors(SelectorCodemodArgs),
    /// Resolve a candidate `source_match` against a chunk and report what it
    /// binds: the matching items, whether it pins a unique target, and (unless
    /// `--no-slack`) which kept values could be holed further without losing
    /// uniqueness. The interactive prove-gate probe for selector authoring.
    ///
    /// Reports `unique` and `matches[]` (`{body_index, binding_name}`
    /// for every top-level statement hit). Candidate selectors use the
    /// public alpha-equivalent identifier policy.
    #[command(name = "match-selector")]
    MatchSelector(MatchSelectorArgs),
    /// Keep-going selector validation: report every selector problem
    /// (no-match, ambiguous, duplicate-claim, resolution error) in one
    /// machine-readable pass.
    ///
    /// The full mode is `debundle run` in dry-run keep-going mode: it
    /// takes the same inputs (`--spec` / `--tree-config` + package
    /// roots) and needs the full pipeline, not just the modules tree —
    /// run it via the Bazel `:debundle` target, not the standalone
    /// binary. `--fail-fast` stops at the first problem. The source-only
    /// preflight mode (`--modules` plus `--source-file` or
    /// `--source-root --chunk`) instead resolves module source selectors
    /// against one chunk in-process — a fast preflight for sharding
    /// selector repairs, without the global selector-assignment backend.
    Validate(ValidateArgs),
}

/// Args for `debundle spec stats`. Source is the on-disk modules tree;
/// totals match what `debundle modules list` + `debundle bindings
/// list` would aggregate when run pairwise.
#[derive(Debug, ClapArgs)]
pub struct SpecStatsArgs {
    /// Modules tree root.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    pub modules_root: PathBuf,

    /// Output format. Default `text` on tty, `json` on pipe. `ndjson`
    /// emits one line per top-level section (`modules`, `bindings`).
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

/// Args for `debundle spec selector-debt`.
#[derive(Debug, ClapArgs)]
pub struct SelectorDebtArgs {
    /// Modules tree root.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    pub modules_root: PathBuf,

    /// Second modules tree to diff minified bindings against. Surfaces
    /// members whose readable `name:` is stable but whose
    /// `selector.binding.name` changed between the two specs.
    #[arg(long = "against")]
    pub against: Option<PathBuf>,

    /// Only list name-only selectors whose minified score is at least
    /// this (0..=100). The summary still counts the whole spec.
    #[arg(long = "min-score", default_value_t = 0)]
    pub min_score: u8,

    /// Group listed name-only selectors by the first N module path components.
    /// Groups are computed after `--min-score` and before `--limit`.
    #[arg(long = "group-module-depth")]
    pub group_module_depth: Option<usize>,

    /// Maximum rows to emit per section. Zero means unlimited.
    #[arg(long, default_value_t = 0)]
    pub limit: usize,

    /// Parse this JS chunk and flag structural selectors with near-matching siblings.
    #[arg(long = "source-file")]
    pub source_file: Option<PathBuf>,

    /// Source root used with `--chunk`.
    #[arg(long = "source-root", env = "DEBUNDLE_SOURCE_ROOT")]
    pub source_root: Option<PathBuf>,

    /// Chunk path relative to `--source-root`, e.g. `static/index.js`.
    #[arg(long = "chunk")]
    pub chunk: Option<PathBuf>,

    /// Minimum source-aware near-match score to report.
    #[arg(long = "near-match-min-score", default_value_t = 55)]
    pub near_match_min_score: usize,

    /// Maximum near-match candidates kept per selector. Zero means unlimited.
    #[arg(long = "near-match-limit", default_value_t = 3)]
    pub near_match_limit: usize,

    /// Output format. Default `text` on tty, `json` on pipe. `ndjson`
    /// emits one tagged object per row plus a final `summary` line.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

#[derive(Debug, Clone, Copy, clap::ValueEnum)]
#[value(rename_all = "kebab-case")]
pub enum SelectorCodemodRewriteArg {
    /// Convert name-only binding members to source_matches selectors.
    NameBindingToSourceMatch,
}

impl From<SelectorCodemodRewriteArg> for SelectorCodemodRewrite {
    fn from(value: SelectorCodemodRewriteArg) -> Self {
        match value {
            SelectorCodemodRewriteArg::NameBindingToSourceMatch => Self::NameBindingToSourceMatch,
        }
    }
}

/// Args for `debundle spec selector-codemod`.
#[derive(Debug, ClapArgs)]
pub struct SelectorCodemodArgs {
    /// Modules tree root.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    pub modules_root: PathBuf,

    /// Rewrite to run. Only canonical source_matches[] synthesis remains after
    /// legacy selector shapes were removed.
    #[arg(long = "rewrite", value_enum, default_value_t = SelectorCodemodRewriteArg::NameBindingToSourceMatch)]
    pub rewrite: SelectorCodemodRewriteArg,

    /// Apply edits. Without this flag the command is a dry run.
    #[arg(long = "apply")]
    pub apply: bool,

    /// Restrict to one or more module YAML files. Relative paths are resolved
    /// under `--modules` when possible.
    #[arg(long = "file")]
    pub files: Vec<PathBuf>,

    /// Restrict to exact module paths, without the `.yaml` suffix.
    #[arg(long = "module")]
    pub modules: Vec<String>,

    /// Restrict to module paths at or below this prefix.
    #[arg(long = "module-prefix")]
    pub module_prefixes: Vec<String>,

    /// Source root used by source-aware rewrites.
    #[arg(long = "source-root")]
    pub source_root: Option<PathBuf>,

    /// Chunk JS path under --source-root for source-aware rewrites.
    #[arg(long = "chunk")]
    pub chunk: Option<PathBuf>,

    /// Direct source JS file for source-aware rewrites.
    #[arg(long = "source-file")]
    pub source_file: Option<PathBuf>,

    /// Restrict source-aware rewrites to module:export items.
    #[arg(long = "item")]
    pub items: Vec<String>,

    /// Emit up to N ranked candidate selectors per item (a menu of alternative
    /// anchors), not just the minimizer's single pick. Only affects
    /// `synthesize-selectors`; the extras are reported as `alternatives`. Default 1.
    #[arg(long = "candidates", default_value_t = 1)]
    pub candidates: usize,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

/// Args for `debundle spec match-selector`.
#[derive(Debug, ClapArgs)]
pub struct MatchSelectorArgs {
    /// Direct source JS file to resolve the selector against.
    #[arg(long = "source-file")]
    pub source_file: Option<PathBuf>,

    /// Source root used with `--chunk`.
    #[arg(long = "source-root", env = "DEBUNDLE_SOURCE_ROOT")]
    pub source_root: Option<PathBuf>,

    /// Chunk path relative to `--source-root`, e.g. `static/index.js`.
    #[arg(long = "chunk")]
    pub chunk: Option<PathBuf>,

    /// The candidate `source_match` `match` text to test. Matched under
    /// the public alpha-equivalent identifier policy.
    #[arg(long = "match")]
    pub match_source: String,

    /// Probe one selector-local binding when the match declares more than one
    /// binding. YAML claim projection lives in `source_matches[].bindings[]`.
    #[arg(long = "target-binding")]
    pub target_binding: Option<String>,

    /// Skip holing-slack analysis (report matches only). Slack is computed by
    /// default when the selector pins a unique target.
    #[arg(long = "no-slack")]
    pub no_slack: bool,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

/// Args for `debundle modules ...`. Aggregates the existing
/// comment-edit verb (in `cli::comment`) with the new
/// `merge` / `delete` / `propose` verbs lifted from `module merge`
/// and the proposal planner.
#[derive(Debug, ClapArgs)]
pub struct ModulesNs {
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
    /// diagnostics; applying them requires `bindings assign` (reviewed
    /// proposal rows feed `bindings assign --batch` directly — see
    /// docs/cli.md § "Batch atomicity"). With `--source-root`, JSON rows
    /// carry `landable_today`, `unaddressable_anonymous_owner_ids`, and
    /// `landability_notes` from full-JS-AST selector uniqueness.
    /// `landable_today` is `false` both for proposals with unaddressable
    /// anonymous statements and for `blocked_residual_dependency`
    /// proposals (outgoing constraining edges into other residual cells)
    /// — the latter need their closure grown or manual co-location
    /// before they can land.
    Propose(PlanWorkArgs),
    /// List all modules in the spec with summary stats.
    ///
    /// Per-row: `member_count`, `anonymous_statement_count`, `residual`
    /// flag, `has_comment`.
    List(ModulesListArgs),
}

/// Top-level `debundle bindings ...` argument node.
#[derive(Debug, ClapArgs)]
pub struct BindingsNs {
    #[command(subcommand)]
    command: BindingsNsCommand,
}

#[derive(Debug, Subcommand)]
enum BindingsNsCommand {
    /// Read, set, edit, or clear a binding's `comment:` field.
    ///
    /// Positional `"text"` replaces the comment; `--edit` opens
    /// `$EDITOR` (fallback `$VISUAL`, then `vi`) on the current text;
    /// `--clear` removes the field; with none of the three, prints the
    /// current comment. An unset comment reads as `null` in JSON (an
    /// empty line in text), distinct from an explicit `comment: ""`.
    /// Member comments emit as JS comment blocks above the binding's
    /// owner statement. Comments don't affect factorization, so
    /// `--no-verify` is a no-op; `--dry-run` previews without writing.
    Comment(BindingCommentArgs),
    /// List every binding in the spec with home module + filters.
    ///
    /// Per-row: minified name, home module, readable name if any, and
    /// `orphan` (sole member of its module) / `unrenamed` flags.
    List(BindingsListNsArgs),
    /// Rename a binding's readable `name:` without moving it.
    ///
    /// Validation is name-collision detection: no two bindings in the
    /// chunk get the same readable name. A convenience over
    /// `bindings assign` for the rename-without-move case; unlike
    /// assign/unassign it also works on `source_matches[].bindings[]`
    /// members.
    Rename(BindingsRenameArgs),
    /// Move one or more bindings into named logical modules atomically.
    ///
    /// Each positional `<sym>:<module>[:<readable>]` moves a binding
    /// and optionally renames it in the same step; `--batch` reads
    /// moves as JSON. Validation runs on the *whole batch's* post-state
    /// and includes the realizability + atom-split gate (the same
    /// predicate `modules merge` / `modules delete --force` use), so a
    /// multi-move refactor whose intermediate states would be invalid
    /// can land in one shot; wanting refuse-intermediate-invalid
    /// semantics means invoking once per move. Destination modules are
    /// auto-created (paths canonicalized/lowercased); batch source
    /// modules drained to zero members are deleted unless they carry a
    /// module-level `comment:`, `source_matches:`, `annotations:`, or
    /// `anonymous_statements:`. Does not yet support moving a
    /// `source_matches[].bindings[]` member out of its claim.
    /// Contract details: docs/cli.md § "Batch atomicity".
    Assign(BindingsAssignArgs),
    /// Remove one or more bindings from their current modules
    /// atomically; they fall through to residual.
    ///
    /// Residual is the default for an owner no spec module claims.
    /// Same post-batch validation, gate, and drained-source-module
    /// sweep as `bindings assign`. Splitting an atom by unassigning
    /// only some of its members is rejected; unassigning a whole atom
    /// together is accepted. Dry-run and apply share an exit code on
    /// the same input.
    Unassign(BindingsUnassignArgs),
}

#[derive(Debug, ClapArgs)]
pub struct BindingsListNsArgs {
    /// Modules tree root.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    pub modules_root: PathBuf,
    /// Restrict to bindings whose home module equals this path.
    #[arg(long = "in")]
    pub in_module: Option<String>,
    /// Restrict to bindings still using their minified name.
    #[arg(long)]
    pub unrenamed: bool,
    /// Restrict to bindings that are the only member of their module.
    #[arg(long)]
    pub orphan: bool,
    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

#[derive(Debug, ClapArgs)]
pub struct BindingsRenameArgs {
    /// Modules tree root.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    pub modules_root: PathBuf,
    /// Current minified or readable name of the binding (must not contain `:`).
    pub original: String,
    /// New readable name (must not contain `:`).
    pub readable: String,
    /// Validate but do not modify any file.
    #[arg(long)]
    pub dry_run: bool,
    /// Skip name-collision validation. Don't use casually.
    #[arg(long)]
    pub no_verify: bool,
    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

#[derive(Debug, ClapArgs)]
pub struct BindingsUnassignArgs {
    /// Modules tree root.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    pub modules_root: PathBuf,
    /// Binding symbols (minified or readable) to remove from their
    /// current modules. Same resolution rules as `bindings assign`.
    #[arg(required = true)]
    pub syms: Vec<String>,
    /// Validate but do not modify any file.
    #[arg(long)]
    pub dry_run: bool,
    /// Skip realizability validation. Don't use casually.
    #[arg(long)]
    pub no_verify: bool,
    /// `owner_graph.json` for the chunk being edited. Required for
    /// the realizability + atom-split gate; ignored when
    /// `--no-verify` is set.
    #[arg(long = "graph", env = "DEBUNDLE_GRAPH")]
    pub owner_graph_path: Option<PathBuf>,
    /// Root used to resolve relative `source_location.source_path`
    /// values when the gate checks anonymous statement selectors.
    #[arg(long = "source-root", env = "DEBUNDLE_SOURCE_ROOT")]
    pub source_root: Option<PathBuf>,
    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

#[derive(Debug, ClapArgs)]
pub struct BindingsAssignArgs {
    /// Modules tree root.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    pub modules_root: PathBuf,
    /// Positional `<sym>:<module>[:<readable>]` triples. May be empty
    /// when `--batch` is supplied.
    ///
    /// `<sym>` accepts the minified or current readable name; the
    /// optional third field sets the new readable `name:` (omitting it
    /// preserves the current one). Neither `<sym>` nor `<readable>` may
    /// contain `:` — use `--batch` JSON for such edge cases.
    pub triples: Vec<String>,
    /// Read additional moves from a JSON file (or `-` for stdin).
    /// Accepts explicit `{sym, module, readable?}` move arrays, plus
    /// reviewed binding-only `modules propose` rows.
    ///
    /// Moves are deduplicated on resolved member identity (duplicates
    /// collapse with a stderr warning); contradictory destinations or
    /// readable names for one member are rejected. Formats and the
    /// propose-row admission rules: docs/cli.md § "Batch atomicity".
    #[arg(long)]
    pub batch: Option<String>,
    /// Validate but do not modify any file.
    #[arg(long)]
    pub dry_run: bool,
    /// Skip realizability + collision validation. Don't use casually.
    #[arg(long)]
    pub no_verify: bool,
    /// `owner_graph.json` for the chunk being edited. Required for
    /// the realizability + atom-split gate; ignored when
    /// `--no-verify` is set.
    #[arg(long = "graph", env = "DEBUNDLE_GRAPH")]
    pub owner_graph_path: Option<PathBuf>,
    /// Root used to resolve relative `source_location.source_path`
    /// values when the gate checks anonymous statement selectors.
    #[arg(long = "source-root", env = "DEBUNDLE_SOURCE_ROOT")]
    pub source_root: Option<PathBuf>,
    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

/// Args for `debundle modules list`.
#[derive(Debug, ClapArgs)]
pub struct ModulesListArgs {
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
    /// predicate) AND no module-level `comment:`. This is the subset
    /// safe to sweep with `modules delete` — a comment would otherwise
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
pub struct ModuleListEntry {
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
pub struct ModulesListReport {
    pub modules: Vec<ModuleListEntry>,
}

/// Args for `debundle describe <id>`.
///
/// `<id>` is dispatched on shape: `owner:NNN`, `atomic:NNN`,
/// `diagnostic:...`, `auto_partition_NNNN`/`extend:...`, a module path
/// (resolves to `<modules>/<id>.yaml`), or otherwise a binding
/// (minified or readable name).
#[derive(Debug, ClapArgs)]
pub struct DescribeArgs {
    /// Identifier to describe.
    pub id: String,

    #[command(flatten)]
    pub common: PeelCommonArgs,

    /// Hard line ceiling used when resolving proposal-id references.
    #[arg(long = "size-cap-lines", default_value_t = DEFAULT_SIZE_CAP_LINES)]
    pub size_cap_lines: usize,

    /// Maximum number of rows to emit per report section. Zero means unlimited.
    #[arg(long, default_value_t = 0)]
    pub limit: usize,

    /// Also run the proposal factorizer to annotate matching proposals and
    /// diagnostics. This is intentionally opt-in because it is expensive on
    /// large graphs.
    #[arg(long = "include-proposals")]
    pub include_proposals: bool,

    /// Root used to resolve relative `source_location.source_path` values.
    #[arg(long = "source-root", env = "DEBUNDLE_SOURCE_ROOT")]
    pub source_root: Option<PathBuf>,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

/// Args for `debundle show-source <id>`.
#[derive(Debug, ClapArgs)]
pub struct ShowSourceArgs {
    /// Identifier to print source text for.
    pub id: String,

    #[command(flatten)]
    pub common: PeelCommonArgs,

    /// Hard line ceiling used when resolving proposal-id references.
    #[arg(long = "size-cap-lines", default_value_t = DEFAULT_SIZE_CAP_LINES)]
    pub size_cap_lines: usize,

    /// Extra source lines around the selected owner span.
    #[arg(long = "context-lines", default_value_t = 20)]
    pub context_lines: usize,

    /// Root used to resolve relative `source_location.source_path` values.
    #[arg(long = "source-root", env = "DEBUNDLE_SOURCE_ROOT")]
    pub source_root: Option<PathBuf>,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

pub fn run_debundle_cli(args: DebundleArgs) -> Result<()> {
    match args.command {
        DebundleCommand::Run(args) => {
            let dry_run = args.dry_run;
            let keep_going = !args.fail_fast;
            let cli = args.resolve()?;
            run_transform_cli_with_options(
                &cli,
                TransformRunOptions {
                    dry_run,
                    keep_going,
                    report_dir_override: None,
                },
            )?;
            if dry_run {
                println!("dry-run: transform pipeline checks passed; no outputs written");
            }
            Ok(())
        }
        DebundleCommand::Bindings(args) => match args.command {
            BindingsNsCommand::Comment(c) => run_binding_comment_cmd(c),
            BindingsNsCommand::List(l) => run_bindings_list_cmd(l),
            BindingsNsCommand::Rename(r) => run_bindings_rename_cmd(r),
            BindingsNsCommand::Assign(a) => run_bindings_assign_cmd(a),
            BindingsNsCommand::Unassign(u) => run_bindings_unassign_cmd(u),
        },
        DebundleCommand::Modules(args) => match args.command {
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
            ModulesNsCommand::List(args) => run_modules_list(args),
        },
        DebundleCommand::Atoms(args) => {
            let report = run_units_report(&args)?;
            emit_report(
                args.format,
                &report,
                render_units_text,
                "writing atoms output",
            )
        }
        DebundleCommand::Coverage(args) => {
            let report = run_patch_plan_report(&args)?;
            emit_report(
                args.format,
                &report,
                render_patch_plan_text,
                "writing coverage output",
            )
        }
        DebundleCommand::GraphSummary(args) => {
            let report = run_graph_summary_report(&args)?;
            emit_report(
                args.format,
                &report,
                render_graph_summary_text,
                "writing graph-summary output",
            )
        }
        DebundleCommand::Describe(args) => run_describe(args),
        DebundleCommand::ShowSource(args) => run_show_source(args),
        DebundleCommand::Scc(args) => run_scc(args),
        DebundleCommand::Cluster(args) => run_cluster(args),
        DebundleCommand::SelectorSolve(args) => run_selector_solve(args),
        DebundleCommand::Spec(args) => match args.command {
            SpecNsCommand::Stats(s) => run_spec_stats_cmd(s),
            SpecNsCommand::SelectorDebt(s) => run_selector_debt_cmd(s),
            SpecNsCommand::SelectorCodemod(s) => run_selector_codemod_cmd(s),
            SpecNsCommand::SynthesizeSelectors(mut s) => {
                s.rewrite = SelectorCodemodRewriteArg::NameBindingToSourceMatch;
                run_selector_codemod_cmd(s)
            }
            SpecNsCommand::MatchSelector(s) => run_match_selector_cmd(s),
            SpecNsCommand::Validate(v) => {
                run_validate_cmd(v).context("running keep-going selector validation")
            }
        },
        // Don't wrap with a generic context — `gate` subcommands
        // already carry enough context in their bail messages (e.g.
        // "no blocking SCC with id ..."), and an outer wrap would
        // hide them since `main.rs` prints only the top context.
        DebundleCommand::Gate(args) => run_gate_cli(args),
    }
}

/// Shared tail of every report subcommand: resolve `format` (tty-aware default),
/// render `report`, and tag IO errors with `context`.
fn emit_report<T, F>(
    format: Option<OutputFormat>,
    report: &T,
    text_render: F,
    context: &'static str,
) -> Result<()>
where
    T: serde::Serialize,
    F: FnOnce(&T, &mut String),
{
    print_report(report, OutputFormat::resolve(format), text_render).context(context)
}

fn run_selector_codemod_cmd(args: SelectorCodemodArgs) -> Result<()> {
    let report = run_selector_codemod(&SelectorCodemodConfig {
        modules_root: args.modules_root,
        apply: args.apply,
        rewrite: args.rewrite.into(),
        files: args.files,
        modules: args.modules,
        module_prefixes: args.module_prefixes,
        source_root: args.source_root,
        chunk: args.chunk,
        source_file: args.source_file,
        items: args.items,
        candidates: args.candidates,
    })?;
    emit_report(
        args.format,
        &report,
        render_selector_codemod_text,
        "writing selector-codemod output",
    )
}

fn run_match_selector_cmd(args: MatchSelectorArgs) -> Result<()> {
    let report = run_match_selector(&MatchSelectorConfig {
        source_file: args.source_file,
        source_root: args.source_root,
        chunk: args.chunk,
        match_source: args.match_source,
        target_binding: args.target_binding,
        check_slack: !args.no_slack,
    })?;
    emit_report(
        args.format,
        &report,
        render_match_selector_text,
        "writing match-selector output",
    )
}

/// Dispatch an `<id>` argument into a [`SelectionArgs`] populated with
/// exactly one field. Module paths and logical module ids resolve
/// through the same owner-graph/spec claim path as other structured
/// IDs, so binding members and anonymous statements stay in sync.
pub fn dispatch_id_selection(id: &str, modules_root: &std::path::Path) -> Result<SelectionArgs> {
    // Prefix-based dispatch covers the structured ID kinds emitted by
    // the analysis crate.
    if id.starts_with("owner:") {
        return Ok(SelectionArgs {
            owner_id: Some(id.to_string()),
            ..SelectionArgs::default()
        });
    }
    if id.starts_with("logical:") {
        return Ok(SelectionArgs {
            module_id: Some(id.to_string()),
            ..SelectionArgs::default()
        });
    }
    if id.starts_with("atomic:") {
        return Ok(SelectionArgs {
            unit_id: Some(id.to_string()),
            ..SelectionArgs::default()
        });
    }
    if id.starts_with("diagnostic:") {
        return Ok(SelectionArgs {
            diagnostic_id: Some(id.to_string()),
            ..SelectionArgs::default()
        });
    }
    // Module-path detection: try resolving `<modules>/<id>.yaml`.
    // Spec authors sometimes have flat module paths (no `/`); the
    // existence check is the only reliable disambiguator vs. binding
    // names that happen to spell a module-like word.
    if let Some(module_path) = resolve_id_as_module_path(id, modules_root)? {
        return Ok(SelectionArgs {
            module_path: Some(module_path),
            ..SelectionArgs::default()
        });
    }
    if id.starts_with("auto_partition_") || id.starts_with("extend:") {
        return Ok(SelectionArgs {
            proposal_id: Some(id.to_string()),
            ..SelectionArgs::default()
        });
    }
    // Fall through: treat as a binding name (minified or readable).
    Ok(SelectionArgs {
        binding_id: Some(id.to_string()),
        ..SelectionArgs::default()
    })
}

fn resolve_id_as_module_path(id: &str, modules_root: &std::path::Path) -> Result<Option<String>> {
    let module_id = id.strip_suffix(".yaml").unwrap_or(id);
    let candidate = modules_root.join(format!("{module_id}.yaml"));
    if candidate.is_file() {
        return Ok(Some(module_id.to_string()));
    }
    if module_id.contains('/') {
        return Ok(None);
    }

    let filename = format!("{module_id}.yaml");
    let mut matches = collect_module_files(modules_root)
        .with_context(|| format!("walking modules tree {}", modules_root.display()))?
        .into_iter()
        .filter(|path| {
            path.file_name()
                .is_some_and(|name| name == filename.as_str())
        })
        .map(|path| module_path_from_file(&path, modules_root));
    let Some(first) = matches.next() else {
        return Ok(None);
    };
    if matches.next().is_some() {
        return Ok(None);
    }
    Ok(Some(first))
}

fn run_describe(args: DescribeArgs) -> Result<()> {
    let selection = dispatch_id_selection(&args.id, &args.common.modules_root)?;
    let inner = ExplainArgs {
        common: args.common,
        selection,
        size_cap_lines: args.size_cap_lines,
        source_root: args.source_root,
        limit: args.limit,
        include_proposals: args.include_proposals,
        format: None,
    };
    let report = run_explain_report(&inner)?;
    emit_report(
        args.format,
        &report,
        render_explain_text,
        "writing describe output",
    )
}

fn run_show_source(args: ShowSourceArgs) -> Result<()> {
    let selection = dispatch_id_selection(&args.id, &args.common.modules_root)?;
    let inner = SourceSliceArgs {
        common: args.common,
        selection,
        size_cap_lines: args.size_cap_lines,
        context_lines: args.context_lines,
        source_root: args.source_root,
        format: None,
    };
    let report = run_source_slice_report(&inner)?;
    emit_report(
        args.format,
        &report,
        render_source_slice_text,
        "writing show-source output",
    )
}

/// Shadow runner: resolve an owner-graph EDB with the in-process Datalog solver
/// and print name-pin categoricity + the derived `aliases` count. With `--check`
/// it is the bootstrap-precondition gate (errors out if name-pin resolution is
/// not total + categorical). See `plans/selector_constraint_model.md`.
fn run_selector_solve(args: SelectorSolveArgs) -> Result<()> {
    let json = std::fs::read_to_string(&args.owner_graph)
        .with_context(|| format!("reading {}", args.owner_graph.display()))?;
    let r = selector_solve::solve_str(&json)
        .with_context(|| format!("parsing owner graph {}", args.owner_graph.display()))?;
    println!("EDB: declares={} uses={}", r.edb_declares, r.edb_uses);
    println!(
        "name-pin: bindings={} unique={} ambiguous={}",
        r.total(),
        r.unique(),
        r.ambiguous()
    );
    println!("aliases (var-decl eager_use): {}", r.aliases.len());

    if args.check {
        let rep = r.shadow_check();
        if !rep.ok() {
            let shown = &rep.ambiguous[..rep.ambiguous.len().min(10)];
            bail!(
                "shadow: FAIL — {} ambiguous binding name(s) block the bootstrap: {shown:?}",
                rep.ambiguous.len(),
            );
        }
        println!(
            "shadow: OK — name-pin resolution total + categorical ({} bindings)",
            rep.total
        );
    }
    Ok(())
}

fn run_bindings_list_cmd(args: BindingsListNsArgs) -> Result<()> {
    let filters = BindingsListFilters {
        in_module: args.in_module,
        unrenamed: args.unrenamed,
        orphan: args.orphan,
    };
    let report = run_bindings_list(&args.modules_root, &filters)?;
    emit_report(
        args.format,
        &report,
        render_bindings_list_text,
        "writing bindings list output",
    )
}

fn render_bindings_list_text(report: &crate::binding::BindingsListReport, out: &mut String) {
    out.push_str(&format!("{} binding(s)\n", report.bindings.len()));
    for entry in &report.bindings {
        let mut flags = Vec::new();
        if entry.orphan {
            flags.push("orphan");
        }
        if !entry.name.is_renamed() {
            flags.push("unrenamed");
        }
        let readable = entry.name.readable().unwrap_or("-");
        out.push_str(&format!(
            "  {}  {}  [{}]  {}\n",
            entry.name.minified(),
            entry.module,
            readable,
            flags.join(",")
        ));
    }
}

fn run_bindings_rename_cmd(args: BindingsRenameArgs) -> Result<()> {
    let out = rename_binding(
        &args.modules_root,
        &args.original,
        &args.readable,
        args.dry_run,
        args.no_verify,
    )?;
    match OutputFormat::resolve(args.format) {
        OutputFormat::Text => {
            let file = out
                .outcome
                .files_written
                .first()
                .map(|f| format!(" ({f})"))
                .unwrap_or_default();
            println!(
                "{}: {} -> {}{file}",
                out.outcome.action,
                out.old_readable.as_deref().unwrap_or(&out.binding),
                out.new_readable,
            );
        }
        format => print_outcome_json(&out, format)?,
    }
    Ok(())
}

fn run_bindings_assign_cmd(args: BindingsAssignArgs) -> Result<()> {
    use std::io::Read;
    let mut moves: Vec<Move> = Vec::new();
    for t in &args.triples {
        moves.push(parse_move_triple(t)?);
    }
    if let Some(path) = &args.batch {
        let text = if path == "-" {
            let mut buf = String::new();
            std::io::stdin().read_to_string(&mut buf)?;
            buf
        } else {
            std::fs::read_to_string(path).with_context(|| format!("reading {path}"))?
        };
        moves.extend(parse_batch_json(&text)?);
    }
    // `Gate::from_cli` is the shared "graph or no-verify" policy
    // every mutating verb uses; no verb can silently skip the
    // realizability gate.
    let gate = Gate::from_cli(
        args.no_verify,
        args.owner_graph_path.as_deref(),
        args.source_root.as_deref(),
    )?;
    let out = match run_bindings_assign(&args.modules_root, moves, args.dry_run, gate) {
        Ok(out) => out,
        Err(err) => {
            emit_gate_rejection_json("assign", args.format, &err);
            return Err(err);
        }
    };
    let format = OutputFormat::resolve(args.format);
    print_assign_outcome(&out, format)
}

fn run_bindings_unassign_cmd(args: BindingsUnassignArgs) -> Result<()> {
    let gate = Gate::from_cli(
        args.no_verify,
        args.owner_graph_path.as_deref(),
        args.source_root.as_deref(),
    )?;
    let out = match run_bindings_unassign(&args.modules_root, args.syms, args.dry_run, gate) {
        Ok(out) => out,
        Err(err) => {
            emit_gate_rejection_json("unassign", args.format, &err);
            return Err(err);
        }
    };
    let format = OutputFormat::resolve(args.format);
    print_unassign_outcome(&out, format)
}

fn print_unassign_outcome(out: &UnassignOutcome, format: OutputFormat) -> Result<()> {
    match format {
        OutputFormat::Text => {
            println!(
                "{}: {} unassign(s); {} file(s) written, {} file(s) deleted",
                out.outcome.action,
                out.unassigned,
                out.outcome.files_written.len(),
                out.outcome.files_deleted.len()
            );
            Ok(())
        }
        format => print_outcome_json(out, format),
    }
}

fn print_assign_outcome(out: &AssignOutcome, format: OutputFormat) -> Result<()> {
    match format {
        OutputFormat::Text => {
            println!(
                "{}: {} move(s); {} file(s) written, {} file(s) deleted",
                out.outcome.action,
                out.moves_applied,
                out.outcome.files_written.len(),
                out.outcome.files_deleted.len()
            );
            Ok(())
        }
        format => print_outcome_json(out, format),
    }
}

fn run_modules_list(args: ModulesListArgs) -> Result<()> {
    use spec_modules::{
        collect_module_files, is_residual_module_path, module_path_from_file, read_module_file,
    };
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
        let claim_count = member_count + module.source_matches.len();
        let entry = ModuleListEntry {
            path,
            member_count,
            anonymous_statement_count: module.anonymous_statements.len(),
            residual,
            has_comment: module.comment.is_some(),
        };
        // `--empty` matches the `modules delete` definition: no claims,
        // annotations, or anonymous statements. A module that carries
        // anonymous statements is not deletable-without-`--force` and isn't
        // empty in any meaningful sense — its rebuild side-effects are still
        // part of the spec.
        let is_truly_empty = claim_count == 0
            && module.annotations.is_empty()
            && entry.anonymous_statement_count == 0;
        let is_auto_deletable = is_truly_empty && !entry.has_comment;
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

fn run_spec_stats_cmd(args: SpecStatsArgs) -> Result<()> {
    let stats = compute_spec_stats(&args.modules_root)?;
    let format = OutputFormat::resolve(args.format);
    match format {
        OutputFormat::Ndjson => {
            // One line per top-level section. Each line is a tagged
            // object so downstream consumers can dispatch on `section`.
            #[derive(serde::Serialize)]
            struct Line<'a, T: serde::Serialize> {
                section: &'a str,
                #[serde(flatten)]
                payload: &'a T,
            }
            println!(
                "{}",
                serde_json::to_string(&Line {
                    section: "modules",
                    payload: &stats.modules,
                })?
            );
            println!(
                "{}",
                serde_json::to_string(&Line {
                    section: "bindings",
                    payload: &stats.bindings,
                })?
            );
            Ok(())
        }
        _ => print_report(&stats, format, render_spec_stats_text_wrapper)
            .context("writing spec stats output"),
    }
}

fn render_spec_stats_text_wrapper(stats: &SpecStats, out: &mut String) {
    render_spec_stats_text(stats, out);
}

fn run_selector_debt_cmd(args: SelectorDebtArgs) -> Result<()> {
    if args.group_module_depth == Some(0) {
        bail!("--group-module-depth must be at least 1");
    }
    let source_file = selector_debt_source_file(&args)?;
    let source_aware = source_file
        .as_deref()
        .map(|source_file| SourceAwareSelectorDebtConfig {
            source_file,
            near_match_min_score: args.near_match_min_score,
            near_match_limit: args.near_match_limit,
        });
    let mut report = compute_selector_debt_with_source(
        &args.modules_root,
        args.against.as_deref(),
        source_aware.as_ref(),
    )?;
    // `--min-score` filters the listed rows; the summary keeps the
    // spec-wide totals so the denominator stays visible.
    if args.min_score > 0 {
        report
            .name_only
            .retain(|entry| entry.minified_score >= args.min_score);
    }
    if let Some(group_module_depth) = args.group_module_depth {
        populate_name_only_module_groups(&mut report, group_module_depth);
    }
    if args.limit > 0 {
        report.name_only.truncate(args.limit);
        report.name_only_module_groups.truncate(args.limit);
        report.repeated_source_match.truncate(args.limit);
        report.drifted_bindings.truncate(args.limit);
        report.source_aware_near_ambiguous.truncate(args.limit);
        report.source_aware_repeated_exact.truncate(args.limit);
        report
            .source_aware_binding_group_suggestions
            .truncate(args.limit);
    }
    let format = OutputFormat::resolve(args.format);
    if format == OutputFormat::Ndjson {
        emit_selector_debt_ndjson(&report)?;
        return Ok(());
    }
    print_report(&report, format, render_selector_debt_text).context("writing selector-debt output")
}

fn selector_debt_source_file(args: &SelectorDebtArgs) -> Result<Option<PathBuf>> {
    match (&args.source_file, &args.chunk) {
        (Some(_), Some(_)) => {
            bail!("use either --source-file or --source-root/--chunk, not both")
        }
        (Some(source_file), None) => Ok(Some(source_file.clone())),
        (None, Some(chunk)) => {
            let Some(source_root) = &args.source_root else {
                bail!("--chunk requires --source-root or DEBUNDLE_SOURCE_ROOT");
            };
            Ok(Some(source_root.join(chunk)))
        }
        (None, None) => Ok(None),
    }
}

/// One tagged JSON object per row, then a final `summary` line — the
/// streaming shape `jq -c` consumers dispatch on via `.section`.
fn emit_selector_debt_ndjson(report: &SelectorDebtReport) -> Result<()> {
    #[derive(serde::Serialize)]
    struct Line<'a, T: serde::Serialize> {
        section: &'a str,
        #[serde(flatten)]
        row: &'a T,
    }
    for entry in &report.name_only {
        println!(
            "{}",
            serde_json::to_string(&Line {
                section: "name_only",
                row: entry,
            })?
        );
    }
    for group in &report.repeated_source_match {
        println!(
            "{}",
            serde_json::to_string(&Line {
                section: "repeated_source_match",
                row: group,
            })?
        );
    }
    for group in &report.name_only_module_groups {
        println!(
            "{}",
            serde_json::to_string(&Line {
                section: "name_only_module_group",
                row: group,
            })?
        );
    }
    for drift in &report.drifted_bindings {
        println!(
            "{}",
            serde_json::to_string(&Line {
                section: "drifted_binding",
                row: drift,
            })?
        );
    }
    for selector in &report.source_aware_near_ambiguous {
        println!(
            "{}",
            serde_json::to_string(&Line {
                section: "source_aware_near_ambiguous",
                row: selector,
            })?
        );
    }
    for group in &report.source_aware_repeated_exact {
        println!(
            "{}",
            serde_json::to_string(&Line {
                section: "source_aware_repeated_exact",
                row: group,
            })?
        );
    }
    for group in &report.source_aware_binding_group_suggestions {
        println!(
            "{}",
            serde_json::to_string(&Line {
                section: "source_aware_binding_group_suggestion",
                row: group,
            })?
        );
    }
    println!(
        "{}",
        serde_json::to_string(&Line {
            section: "summary",
            row: &report.summary,
        })?
    );
    Ok(())
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

// --- Text renderers -------------------------------------------------
//
// Each query command needs a text rendering for tty output. v1 keeps
// these compact: one line per item plus a brief summary header.

fn render_units_text(report: &peel::UnitsReport, out: &mut String) {
    out.push_str(&format!("{} atom(s)\n", report.units.len()));
    for unit in &report.units {
        let bindings: Vec<&str> = unit.members.iter().map(|m| m.binding.as_str()).collect();
        out.push_str(&format!(
            "  {}  [{}]  size={}\n",
            unit.id,
            bindings.join(", "),
            unit.size_lines_estimate
        ));
    }
}

fn render_patch_plan_text(report: &peel::PatchPlanReport, out: &mut String) {
    out.push_str(&format!(
        "{} patch set(s): {} complete, {} split, {} unknown bindings\n",
        report.summary.total_patch_sets,
        report.summary.complete_patch_sets,
        report.summary.split_patch_sets,
        report.summary.unknown_binding_count,
    ));
    for row in &report.rows {
        out.push_str(&format!("  {} [{:?}]\n", row.path, row.status));
    }
}

fn render_graph_summary_text(report: &peel::GraphSummaryReport, out: &mut String) {
    let proposal_count = report
        .proposal_count
        .map(|count| count.to_string())
        .unwrap_or_else(|| "skipped".to_string());
    let diagnostic_count = report
        .diagnostic_count
        .map(|count| count.to_string())
        .unwrap_or_else(|| "skipped".to_string());
    out.push_str(&format!(
        "owners={} edges={} atoms={} residual={} proposals={} diagnostics={}\n",
        report.owner_count,
        report.owner_edge_count,
        report.atomic_unit_count,
        report.residual_atomic_unit_count,
        proposal_count,
        diagnostic_count,
    ));
}

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

fn render_explain_text(report: &peel::ExplainReport, out: &mut String) {
    out.push_str(&format!(
        "{:?} {:?}\n",
        report.query.kind, report.query.value
    ));
    out.push_str(&format!("  owners: {}\n", report.owner_ids.join(", ")));
    out.push_str(&format!(
        "  bindings: {}\n",
        report
            .bindings
            .iter()
            .map(|b| b.binding.as_str())
            .collect::<Vec<_>>()
            .join(", ")
    ));
    // Home module path per binding (CLI_DOGFOOD #6): the JSON carries
    // `binding_homes[].path`, but the text view previously dropped it,
    // leaving no way to see where a binding lives without `--format json`.
    if !report.binding_homes.is_empty() {
        out.push_str("  homes:\n");
        for home in &report.binding_homes {
            out.push_str(&format!("    {} -> {}\n", home.binding, home.path));
        }
    }
    if !report.unknown_binding_ids.is_empty() {
        out.push_str(&format!(
            "  unknown bindings (claimed but absent from owner graph): {}\n",
            report.unknown_binding_ids.join(", ")
        ));
    }
    out.push_str(&format!("  atomic_units: {}\n", report.atomic_units.len()));
    out.push_str(&format!(
        "  incoming_edges: {}, outgoing_edges: {}\n",
        report.incoming_edges.len(),
        report.outgoing_edges.len()
    ));
}

fn render_source_slice_text(report: &peel::SourceSliceReport, out: &mut String) {
    for slice in &report.slices {
        out.push_str(&format!(
            "--- {} (lines {}-{}) ---\n",
            slice.source_path, slice.context_start_line, slice.context_end_line
        ));
        out.push_str(&slice.text);
        if !slice.text.ends_with('\n') {
            out.push('\n');
        }
    }
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::{Path, PathBuf};

    use clap::Parser;
    use pipeline::{TransformArgs, TransformSpecSource};
    use tempfile::TempDir;

    use super::DebundleArgs;

    fn write(root: &Path, rel: &str, body: &str) {
        let path = root.join(rel);
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).unwrap();
        }
        fs::write(path, body).unwrap();
    }

    fn parsed_run_args(argv: &[&str]) -> TransformArgs {
        let parsed = DebundleArgs::try_parse_from(argv).expect("parse cli");
        match parsed.command {
            super::DebundleCommand::Run(args) => args,
            other => panic!("expected run command, got {other:?}"),
        }
    }

    #[test]
    fn parse_run_args_matches_js_surface() {
        js_ast::with_swc_globals(|| {
            let args = parsed_run_args(&[
                "debundle",
                "run",
                "--spec",
                "spec.yaml",
                "--dry-run",
                "--package-root",
                "pkg=/tmp/pkg",
                "--packages-root",
                "/tmp/packages",
            ]);
            assert!(args.dry_run);
            assert!(!args.fail_fast);
            let cli = args.resolve().expect("resolve cli");
            assert_eq!(
                cli.spec_source,
                TransformSpecSource::Flat {
                    path: PathBuf::from("spec.yaml")
                }
            );
            assert_eq!(
                cli.package_roots.get("pkg"),
                Some(&PathBuf::from("/tmp/pkg"))
            );
            assert_eq!(cli.packages_root, Some(PathBuf::from("/tmp/packages")));
        });
    }

    #[test]
    fn parse_run_args_accepts_fail_fast_opt_out() {
        js_ast::with_swc_globals(|| {
            let args = parsed_run_args(&["debundle", "run", "--spec", "spec.yaml", "--fail-fast"]);
            assert!(args.fail_fast);
            assert!(!args.keep_going);
        });
    }

    #[test]
    fn parse_tree_run_args() {
        js_ast::with_swc_globals(|| {
            let args = parsed_run_args(&[
                "debundle",
                "run",
                "--tree-config",
                "spec_config.yaml",
                "--tree-modules",
                "modules",
                "--tree-vendor-marks",
                "vendor_marks.yaml",
                "--tree-source-root",
                "/workspace",
                "--out-root",
                "out",
            ]);
            let cli = args.resolve().expect("resolve cli");
            assert_eq!(
                cli.spec_source,
                TransformSpecSource::Tree(spec_tree::CompileSpecTreeOptions {
                    config_path: PathBuf::from("spec_config.yaml"),
                    modules_root: PathBuf::from("modules"),
                    vendor_marks_path: PathBuf::from("vendor_marks.yaml"),
                    source_root: Some(PathBuf::from("/workspace")),
                    out_root: PathBuf::from("out"),
                })
            );
        });
    }

    #[test]
    fn deprecated_command_roots_are_rejected() {
        for args in [
            &["debundle", "peel", "plan-work"][..],
            &["debundle", "peel", "units"],
            &["debundle", "peel", "patch-plan"],
            &["debundle", "peel", "explain"],
            &["debundle", "peel", "source-slice"],
            &["debundle", "peel", "graph-summary"],
            &["debundle", "module", "merge"],
        ] {
            let error = DebundleArgs::try_parse_from(args).expect_err("alias should be rejected");
            assert_eq!(error.kind(), clap::error::ErrorKind::InvalidSubcommand);
        }
    }

    #[test]
    fn parse_top_level_atoms_command() {
        let parsed = DebundleArgs::try_parse_from([
            "debundle",
            "atoms",
            "--graph",
            "owner_graph.json",
            "--modules",
            "modules",
        ])
        .expect("parse cli");
        assert!(matches!(parsed.command, super::DebundleCommand::Atoms(_)));
    }

    #[test]
    fn parse_top_level_coverage_command() {
        let parsed = DebundleArgs::try_parse_from([
            "debundle",
            "coverage",
            "--graph",
            "owner_graph.json",
            "--modules",
            "modules",
        ])
        .expect("parse cli");
        assert!(matches!(
            parsed.command,
            super::DebundleCommand::Coverage(_)
        ));
    }

    #[test]
    fn parse_top_level_graph_summary_command() {
        let parsed = DebundleArgs::try_parse_from([
            "debundle",
            "graph-summary",
            "--graph",
            "owner_graph.json",
            "--modules",
            "modules",
        ])
        .expect("parse cli");
        assert!(matches!(
            parsed.command,
            super::DebundleCommand::GraphSummary(_)
        ));
    }

    #[test]
    fn parse_top_level_describe_command() {
        let parsed = DebundleArgs::try_parse_from([
            "debundle",
            "describe",
            "XOe",
            "--graph",
            "owner_graph.json",
            "--modules",
            "modules",
        ])
        .expect("parse cli");
        assert!(matches!(
            parsed.command,
            super::DebundleCommand::Describe(_)
        ));
    }

    #[test]
    fn render_explain_text_includes_home_module_paths() {
        // CLI_DOGFOOD #6: the text view must surface each binding's home
        // module path (the JSON's `binding_homes[].path`), not only owners
        // / bindings / atom / edge counts.
        use peel::plan::{BindingHomeReport, BindingHomeSourceKind, QueryKind, QueryReport};
        let report = peel::ExplainReport {
            query: QueryReport {
                kind: QueryKind::Binding,
                value: "XOe".to_string(),
            },
            owner_ids: vec!["owner:0".to_string()],
            owners: vec![],
            neighbor_owners: vec![],
            bindings: vec![],
            binding_homes: vec![BindingHomeReport {
                binding: "XOe".to_string(),
                name: "PluginSettingsAccessor".to_string(),
                source_kind: BindingHomeSourceKind::Module,
                path: "runtime/plugins".to_string(),
            }],
            incoming_edges: vec![],
            outgoing_edges: vec![],
            atomic_units: vec![],
            incoming_atomic_edges: vec![],
            outgoing_atomic_edges: vec![],
            quotient_edges: vec![],
            unknown_binding_ids: vec![],
            factorize_proposals: None,
            factorize_diagnostics: None,
            limits: None,
        };
        let mut out = String::new();
        super::render_explain_text(&report, &mut out);
        assert!(out.contains("homes:"), "missing homes section:\n{out}");
        assert!(
            out.contains("XOe -> runtime/plugins"),
            "missing binding->path line:\n{out}",
        );
    }

    #[test]
    fn parse_top_level_show_source_command() {
        let parsed = DebundleArgs::try_parse_from([
            "debundle",
            "show-source",
            "XOe",
            "--graph",
            "owner_graph.json",
            "--modules",
            "modules",
            "--source-root",
            "/snapshot",
        ])
        .expect("parse cli");
        assert!(matches!(
            parsed.command,
            super::DebundleCommand::ShowSource(_)
        ));
    }

    #[test]
    fn dispatch_id_selection_resolves_unique_module_basename_before_proposal_id() {
        let dir = TempDir::new().unwrap();
        let modules_root = dir.path().join("modules");
        write(
            &modules_root,
            "auto_partition/auto_partition_0499.yaml",
            "members: []\n",
        );

        let selection = super::dispatch_id_selection("auto_partition_0499", &modules_root).unwrap();

        assert_eq!(
            selection.module_path.as_deref(),
            Some("auto_partition/auto_partition_0499")
        );
        assert!(selection.proposal_id.is_none());
    }

    #[test]
    fn parse_bindings_comment_command() {
        let parsed = DebundleArgs::try_parse_from([
            "debundle",
            "bindings",
            "comment",
            "--modules",
            "spec/modules",
            "XOe",
            "hand-written annotation",
        ])
        .expect("parse cli");
        assert!(matches!(
            parsed.command,
            super::DebundleCommand::Bindings(_)
        ));
    }

    #[test]
    fn parse_modules_comment_command() {
        let parsed = DebundleArgs::try_parse_from([
            "debundle",
            "modules",
            "comment",
            "--modules",
            "spec/modules",
            "runtime/plugins",
            "--clear",
        ])
        .expect("parse cli");
        assert!(matches!(parsed.command, super::DebundleCommand::Modules(_)));
    }

    #[test]
    fn dispatch_id_owner_prefix() {
        let tmp = tempfile::tempdir().unwrap();
        let modules = tmp.path().to_path_buf();
        std::fs::create_dir_all(&modules).unwrap();
        let sel = super::dispatch_id_selection("owner:42", &modules).unwrap();
        assert_eq!(sel.owner_id.as_deref(), Some("owner:42"));
    }

    #[test]
    fn dispatch_id_logical_prefix() {
        let tmp = tempfile::tempdir().unwrap();
        let sel = super::dispatch_id_selection("logical:7", tmp.path()).unwrap();
        assert_eq!(sel.module_id.as_deref(), Some("logical:7"));
    }

    #[test]
    fn dispatch_id_atomic_prefix() {
        let tmp = tempfile::tempdir().unwrap();
        let sel = super::dispatch_id_selection("atomic:7", tmp.path()).unwrap();
        assert_eq!(sel.unit_id.as_deref(), Some("atomic:7"));
    }

    #[test]
    fn dispatch_id_diagnostic_prefix() {
        let tmp = tempfile::tempdir().unwrap();
        let sel = super::dispatch_id_selection("diagnostic:size_cap_0001", tmp.path()).unwrap();
        assert_eq!(
            sel.diagnostic_id.as_deref(),
            Some("diagnostic:size_cap_0001")
        );
    }

    #[test]
    fn dispatch_id_proposal_prefix() {
        let tmp = tempfile::tempdir().unwrap();
        let sel = super::dispatch_id_selection("auto_partition_0042", tmp.path()).unwrap();
        assert_eq!(sel.proposal_id.as_deref(), Some("auto_partition_0042"));
    }

    #[test]
    fn dispatch_id_module_path_when_yaml_exists() {
        let tmp = tempfile::tempdir().unwrap();
        let modules = tmp.path();
        std::fs::create_dir_all(modules.join("runtime")).unwrap();
        std::fs::write(modules.join("runtime/plugins.yaml"), "members: []\n").unwrap();
        let sel = super::dispatch_id_selection("runtime/plugins", modules).unwrap();
        assert_eq!(sel.module_path.as_deref(), Some("runtime/plugins"));
    }

    #[test]
    fn dispatch_id_binding_otherwise() {
        let tmp = tempfile::tempdir().unwrap();
        let sel = super::dispatch_id_selection("XOe", tmp.path()).unwrap();
        assert_eq!(sel.binding_id.as_deref(), Some("XOe"));
    }

    #[test]
    fn parse_top_level_gate_list_command() {
        let parsed = DebundleArgs::try_parse_from([
            "debundle",
            "gate",
            "list",
            "--graph",
            "owner_graph.json",
            "--modules",
            "modules",
        ])
        .expect("parse cli");
        assert!(matches!(parsed.command, super::DebundleCommand::Gate(_)));
    }

    #[test]
    fn parse_top_level_gate_describe_command() {
        let parsed = DebundleArgs::try_parse_from([
            "debundle",
            "gate",
            "describe",
            "0",
            "--graph",
            "owner_graph.json",
            "--modules",
            "modules",
            "--binding",
            "XOe",
        ])
        .expect("parse cli");
        assert!(matches!(parsed.command, super::DebundleCommand::Gate(_)));
    }

    #[test]
    fn parse_top_level_gate_cut_command_with_cycles_override() {
        let parsed = DebundleArgs::try_parse_from([
            "debundle",
            "gate",
            "cut",
            "3",
            "--graph",
            "owner_graph.json",
            "--modules",
            "modules",
            "--cycles",
            "/other/cycles.json",
        ])
        .expect("parse cli");
        assert!(matches!(parsed.command, super::DebundleCommand::Gate(_)));
    }
}
