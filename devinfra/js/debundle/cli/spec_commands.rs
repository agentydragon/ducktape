//! `spec` authoring commands: arguments, adapters, and output formats.
use crate::validate::{ValidateArgs, run_validate_cmd};
use crate::{emit_report, print_section};
use anyhow::{Context, Result, bail};
use clap::{Args as ClapArgs, Subcommand};
use peel::{OutputFormat, print_report};
use selector_codemod::match_selector::{
    MatchSelectorConfig, render_match_selector_text, run_match_selector,
};
use selector_codemod::{SelectorCodemodConfig, render_selector_codemod_text, run_selector_codemod};
use selector_debt::{
    SelectorDebtReport, SourceAwareSelectorDebtConfig, compute_selector_debt_with_source,
    populate_name_only_module_groups, render_selector_debt_text,
};
use spec_stats::{compute_spec_stats, render_spec_stats_text};
use std::path::PathBuf;

/// Args for `debundle spec ...`.
#[derive(Debug, ClapArgs)]
pub(super) struct SpecNs {
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
    /// Synthesize structural selectors for selected name-only members.
    ///
    /// Dry-run by default; `--apply` writes. Scope the run with `--file`,
    /// `--module`, `--module-prefix`, or `--item`. Skipped rows carry a
    /// machine-readable reason. Given module exports, produce a
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
    /// Resolve a candidate `source_match` alone against a chunk and report
    /// its outcome and (unless `--no-slack`) which kept values could be
    /// holed further without losing uniqueness. The interactive prove-gate
    /// probe for selector authoring.
    ///
    /// Reports one selector outcome record (kinds: SPEC.md § Outcomes) in
    /// the `{counts, outcomes}` format `spec validate` uses, plus `slack`
    /// when resolved. Candidate selectors use the public alpha-equivalent
    /// identifier policy.
    #[command(name = "match-selector")]
    MatchSelector(MatchSelectorArgs),
    /// Keep-going selector validation: report every selector outcome that is
    /// not ok (kinds: SPEC.md § Outcomes) and what each matched template's
    /// free identifiers mean, in one machine-readable pass.
    ///
    /// The full mode is `debundle run` in dry-run keep-going mode: it
    /// takes the same inputs (`--spec` / `--tree-config` + package
    /// roots) and needs the full pipeline, not just the modules tree —
    /// run it via the Bazel `:debundle` target, not the standalone
    /// binary. `--fail-fast` stops at the first problem. The source-only
    /// preflight mode (`--modules` plus `--source-file` or
    /// `--source-root --chunk`) instead resolves the module files jointly
    /// against one chunk, with the same resolve as `run`, but without the
    /// pipeline build — a fast preflight for sharding selector repairs.
    Validate(ValidateArgs),
}

/// Args for `debundle spec stats`. Source is the on-disk modules tree;
/// totals match what `debundle modules list` + `debundle bindings
/// list` would aggregate when run pairwise.
#[derive(Debug, ClapArgs)]
struct SpecStatsArgs {
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
struct SelectorDebtArgs {
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

/// Args for `debundle spec synthesize-selectors`.
#[derive(Debug, ClapArgs)]
struct SelectorCodemodArgs {
    /// Modules tree root.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    pub modules_root: PathBuf,

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
    /// anchors), not just the minimizer's single pick; the extras are reported
    /// as `alternatives`. Default 1.
    #[arg(long = "candidates", default_value_t = 1)]
    pub candidates: usize,

    /// Output format. Default `text` on tty, `json` on pipe. `ndjson` flushes
    /// each completed module, followed by a final summary.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

/// Args for `debundle spec match-selector`.
#[derive(Debug, ClapArgs)]
struct MatchSelectorArgs {
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
    #[arg(
        long = "match",
        conflicts_with = "selector",
        required_unless_present = "selector"
    )]
    pub match_source: Option<String>,

    /// Authored entry: FILE.yaml#/source_matches/INDEX or /anonymous_statements/INDEX.
    #[arg(long, requires = "explain")]
    pub selector: Option<String>,

    /// Read --selector '#/JSON/Pointer' from this YAML file, without resolving its spec.
    #[arg(long, requires = "selector")]
    pub spec: Option<PathBuf>,

    /// Compare only the selected range. Does not solve ownership or other spec constraints.
    #[arg(long, requires = "statements")]
    pub explain: bool,

    /// Inclusive zero-based source statement range, as printed by inspect-source.
    #[arg(long, requires = "explain")]
    pub statements: Option<source_inspection::StatementRange>,

    /// Use anonymous-statement sequence semantics for an inline --match.
    #[arg(long, requires = "explain", conflicts_with_all = ["selector", "target_binding"])]
    pub anonymous: bool,

    /// Probe one selector-local binding when the match declares more than one
    /// binding. YAML claim projection lives in `source_matches[].bindings[]`.
    #[arg(long = "target-binding")]
    pub target_binding: Option<String>,

    /// Skip holing-slack analysis (report the outcome only). Slack is
    /// computed by default when the selector pins a unique target.
    #[arg(long = "no-slack", conflicts_with = "explain")]
    pub no_slack: bool,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

fn run_synthesize_selectors_cmd(args: SelectorCodemodArgs) -> Result<()> {
    let format = OutputFormat::resolve(args.format);
    let config = SelectorCodemodConfig {
        modules_root: args.modules_root,
        apply: args.apply,
        files: args.files,
        modules: args.modules,
        module_prefixes: args.module_prefixes,
        source_root: args.source_root,
        chunk: args.chunk,
        source_file: args.source_file,
        items: args.items,
        candidates: args.candidates,
    };
    if format == OutputFormat::Ndjson {
        let report =
            selector_codemod::run_selector_codemod_with_module_results(&config, &mut |module| {
                print_section("module", module)?;
                std::io::Write::flush(&mut std::io::stdout())?;
                Ok(())
            })?;
        return print_section("summary", &report.summary);
    }
    let report = run_selector_codemod(&config)?;
    emit_report(
        args.format,
        &report,
        render_selector_codemod_text,
        "writing synthesize-selectors output",
    )
}

fn run_match_selector_cmd(args: MatchSelectorArgs) -> Result<()> {
    if args.explain {
        let config = crate::selector_explanation::ExplanationConfig {
            source_file: args.source_file,
            source_root: args.source_root,
            chunk: args.chunk,
            match_source: args.match_source,
            selector: args.selector,
            spec_file: args.spec,
            target_binding: args.target_binding,
            anonymous: args.anonymous,
            statements: args.statements.context("--explain requires --statements")?,
        };
        let report = crate::selector_explanation::explain(&config)?;
        return emit_report(
            args.format,
            &report,
            crate::selector_explanation::render,
            "writing selector explanation",
        );
    }
    let report = run_match_selector(&MatchSelectorConfig {
        source_file: args.source_file,
        source_root: args.source_root,
        chunk: args.chunk,
        match_source: args.match_source.context("--match is required")?,
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

fn run_spec_stats_cmd(args: SpecStatsArgs) -> Result<()> {
    let stats = compute_spec_stats(&args.modules_root)?;
    let format = OutputFormat::resolve(args.format);
    match format {
        OutputFormat::Ndjson => {
            print_section("modules", &stats.modules)?;
            print_section("bindings", &stats.bindings)?;
            Ok(())
        }
        _ => print_report(&stats, format, render_spec_stats_text)
            .context("writing spec stats output"),
    }
}

fn run_selector_debt_cmd(args: SelectorDebtArgs) -> Result<()> {
    if args.group_module_depth == Some(0) {
        bail!("--group-module-depth must be at least 1");
    }
    let source_file = selector_codemod::source_input::optional_chunk_source_file(
        args.source_file.as_deref(),
        args.source_root.as_deref(),
        args.chunk.as_deref(),
    )?;
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

/// One tagged JSON object per row, then a final `summary` line — the
/// streaming shape `jq -c` consumers dispatch on via `.section`.
fn emit_selector_debt_ndjson(report: &SelectorDebtReport) -> Result<()> {
    for row in &report.name_only {
        print_section("name_only", row)?;
    }
    for row in &report.repeated_source_match {
        print_section("repeated_source_match", row)?;
    }
    for row in &report.name_only_module_groups {
        print_section("name_only_module_group", row)?;
    }
    for row in &report.drifted_bindings {
        print_section("drifted_binding", row)?;
    }
    for row in &report.source_aware_near_ambiguous {
        print_section("source_aware_near_ambiguous", row)?;
    }
    for row in &report.source_aware_repeated_exact {
        print_section("source_aware_repeated_exact", row)?;
    }
    for row in &report.source_aware_binding_group_suggestions {
        print_section("source_aware_binding_group_suggestion", row)?;
    }
    print_section("summary", &report.summary)
}

pub(super) fn run(args: SpecNs) -> Result<()> {
    match args.command {
        SpecNsCommand::Stats(s) => run_spec_stats_cmd(s),
        SpecNsCommand::SelectorDebt(s) => run_selector_debt_cmd(s),
        SpecNsCommand::SynthesizeSelectors(s) => run_synthesize_selectors_cmd(s),
        SpecNsCommand::MatchSelector(s) => run_match_selector_cmd(s),
        SpecNsCommand::Validate(v) => {
            run_validate_cmd(v).context("running keep-going selector validation")
        }
    }
}
