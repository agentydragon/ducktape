//! `debundle spec propose-commutations` — draft `commuting_statements`
//! entries for the `sequenced` edges inside a rejection's blocking SCCs
//! (SPEC.md § Commuting statements, § Realizability cut explanations).
//!
//! The command reads `cycles.json` and `dependency_graph.json.gz`, classifies
//! each constraining `sequenced` edge between an SCC's modules with the
//! verdict the graph build retained on it
//! (`edges[].premise_verdict`, decided by `graph/build/commuting.rs::premise_verdict`,
//! the same value the gate applies), synthesizes a proven selector for each
//! endpoint, and emits a YAML draft plus a report. It writes only the draft
//! (`--out`); applying an entry is the author's step, after which the gate's
//! own contradiction and unused checks validate it.

mod render;
mod report;
mod scc_analysis;

use crate::propose_commutations::render::{render_draft, render_text};
use crate::propose_commutations::report::{ProposeCommutationsReport, ReportTotals};
use crate::propose_commutations::scc_analysis::Analysis;
use anyhow::{Context, Result};
use clap::Args as ClapArgs;
use command_output::emit_report;
use graph::DependencyGraphInput;
use graph_compatibility::GraphCompatibilityScope;
use graph_provenance::CurrentRoots;
use graph_provenance::VerifiedGraphSourceContext;
use peel::OutputFormat;
use source_inspection::PreparedSource;
use spec::ModulePath;
use spec_modules::{collect_module_files, module_path_from_file};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

/// Args for `debundle spec propose-commutations`.
#[derive(Debug, ClapArgs)]
pub(super) struct ProposeCommutationsArgs {
    /// `dependency_graph.json.gz` of the rejected chunk.
    #[arg(long = "graph", env = "DEBUNDLE_GRAPH")]
    pub graph: PathBuf,

    /// Override the default `cycles.json` location (the sibling of `--graph`).
    #[arg(long = "cycles")]
    pub cycles: Option<PathBuf>,

    /// Selector lookup root containing the graph's verified input file.
    #[arg(long = "source-root", env = "DEBUNDLE_SOURCE_ROOT")]
    pub source_root: PathBuf,

    /// The chunk's module YAML tree. With it, an endpoint that is a logical
    /// member is written as `resolved_member` under its authored module
    /// path; without it every endpoint gets a synthesized `source_match`.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    pub modules: Option<PathBuf>,

    /// Write the YAML draft to this file (the report still goes to stdout).
    #[arg(long = "out")]
    pub out_path: Option<PathBuf>,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,

    /// Emit at most this many proposals, rejected pairs and co-location rows
    /// each, best first; `totals` counts all of them.
    #[arg(long, default_value_t = 50, conflicts_with = "all")]
    pub limit: usize,

    /// Emit every proposal, rejected pair and co-location row, proving a
    /// selector for every endpoint.
    #[arg(long)]
    pub all: bool,
}

impl ProposeCommutationsArgs {
    pub(super) fn resolved_cycles_path(&self) -> PathBuf {
        self.cycles
            .clone()
            .unwrap_or_else(|| crate::gate::sibling_cycles_path(&self.graph))
    }

    pub(super) fn verify_graph(&self, roots: &CurrentRoots) -> Result<VerifiedGraphSourceContext> {
        let scope = match &self.modules {
            Some(_) => GraphCompatibilityScope::AuthoredModuleTree { tree_config: None },
            None => GraphCompatibilityScope::ExactSourceUnit,
        };
        graph_compatibility::verify_graph_compatibility_with_scope(
            &self.graph,
            self.modules.as_deref(),
            Some(self.source_root.as_path()),
            roots,
            scope,
        )
    }
}

pub(super) fn run(
    args: ProposeCommutationsArgs,
    context: &VerifiedGraphSourceContext,
    output: &mut dyn std::io::Write,
) -> Result<()> {
    let cycles_path = args.resolved_cycles_path();
    let sccs = crate::gate::load_blocking_sccs(&cycles_path)?;
    let input = DependencyGraphInput::read(&args.graph)?;
    let report = input.report();
    let authored_modules = match &args.modules {
        Some(root) => Some(authored_module_paths(root)?),
        None => None,
    };
    let source = PreparedSource::from_source(
        context.input_file(),
        context.source_path(),
        context.source().to_owned(),
    )?;
    let limit = (!args.all).then_some(args.limit);
    let mut analysis = Analysis::new(&input, &source, authored_modules.as_ref())?;
    let (mut needs_co_location, mut rejected) = analysis.collect_candidates(&sccs)?;
    let ranked = analysis.rank_candidates();
    let (proposals, proposal_total) = analysis.emit_proposals(ranked, limit, &mut rejected)?;
    let scc_outcomes = analysis.scc_outcomes()?;
    let totals = ReportTotals {
        proposals: proposal_total,
        rejected: rejected.len(),
        needs_co_location: needs_co_location.len(),
    };
    if let Some(limit) = limit {
        rejected.truncate(limit);
        needs_co_location.truncate(limit);
    }
    let draft_yaml = render_draft(&report.chunk_id, &proposals)?;
    let draft_written = match &args.out_path {
        Some(path) => {
            std::fs::write(path, &draft_yaml)
                .with_context(|| format!("writing draft {}", path.display()))?;
            Some(path.clone())
        }
        None => None,
    };
    let report = ProposeCommutationsReport {
        chunk: report.chunk_id.clone(),
        graph: args.graph.clone(),
        cycles: cycles_path,
        sccs: scc_outcomes,
        totals,
        proposals,
        needs_co_location,
        rejected,
        draft_yaml,
        draft_written,
    };
    emit_report(
        output,
        args.format,
        &report,
        render_text,
        "writing propose-commutations output",
    )
}

/// Canonical module path → authored spelling, for every YAML module of the
/// tree. `resolved_member.module` must use the authored spelling (the
/// resolver keys member sites by it), which the graph's canonical,
/// case-folded paths cannot supply.
fn authored_module_paths(modules_root: &Path) -> Result<BTreeMap<ModulePath, String>> {
    let mut out = BTreeMap::new();
    for file in collect_module_files(modules_root)? {
        let authored = module_path_from_file(&file, modules_root);
        let canonical = ModulePath::parse(&authored, "")
            .with_context(|| format!("module YAML {} path", file.display()))?;
        out.insert(canonical, authored);
    }
    Ok(out)
}
