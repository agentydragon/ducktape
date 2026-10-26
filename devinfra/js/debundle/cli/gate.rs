//! `debundle gate` — query the realizability gate's rejected SCCs.
//!
//! When `materialize_logical_modules` rejects a spec, it writes one
//! entry per blocking SCC to `reports/tree/<chunk>/cycles.json` and
//! exits with a stderr summary. The trimmed wire shape (see
//! [`BlockingSccEntry`] and `docs/wire_format.md`) carries
//! `id` / `modules` / `cut` per SCC — enough to dispatch follow-up
//! queries without storing the full evidence block on disk.
//!
//! This CLI surfaces three read-only views over that data:
//!
//! - `gate list` — one line per blocking SCC (id, modules count, cut size).
//! - `gate describe <id>` — full picture: modules list, cut, and
//!   **recomputed evidence**, sourced from `dependency_graph.json` plus
//!   the SCC's module set. Same per-binding-pair render as the
//!   per-rejection stderr summary.
//! - `gate cut <id>` — just the cut edges (already in the trimmed
//!   `cycles.json`). The actionable subset spec authors edit.
//!
//! Name choice: `gate` reads well in error messages ("the gate
//! rejected; run `debundle gate list` for details") and disambiguates
//! from the generic `scc` command, which lists every quotient SCC.
//! The unit is the **blocking** SCC — a multi-module SCC with at
//! least one realizability-constraining cross-module edge — not
//! arbitrary cycles, of which a single SCC can contain exponentially
//! many.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};

use ::gate::{BlockingSccEntry, CycleEdge};
use analysis::{
    DepKind, DependencyGraphReport, EdgeRoleReport, ModuleKey, Purity, SequencedSourceUnitCause,
    StatementOrdinal,
};
use anyhow::{Context, Result};
use clap::{Args as ClapArgs, Subcommand};
use serde::Serialize;
use spec::ModulePath;
use swc_atoms::Atom;

/// Top-level `debundle gate ...` argument node.
#[derive(Debug, ClapArgs)]
pub struct GateArgs {
    #[command(subcommand)]
    command: GateCommand,
}

#[derive(Debug, Subcommand)]
enum GateCommand {
    /// List every blocking SCC. One row per entry in `cycles.json`.
    ///
    /// A missing `cycles.json` is the clean state — the pipeline and
    /// the edit gate write it only on rejection, and a passing edit
    /// gate clears a stale one — so it reports zero blocking SCCs
    /// (`[]`) and exits 0, distinct from a present-but-malformed file
    /// (which errors).
    List(GateListArgs),
    /// Full picture for one blocking SCC: modules, cut, recomputed evidence.
    ///
    /// The per-edge evidence is recomputed on demand from
    /// `dependency_graph.json` and the SCC's module set — the same
    /// per-binding-pair blame view the realizability gate emits to
    /// stderr at rejection time.
    Describe(GateDescribeArgs),
    /// Just the cut edges for one blocking SCC. The actionable subset —
    /// spec authors read this to pick which back-edge to break.
    Cut(GateCutArgs),
}

/// Paths for reading blocking SCCs. List/cut need only the cycles report;
/// describe additionally requires the graph to recompute per-edge evidence.
///
/// `cycles.json` defaults to the sibling of `--graph` (the standard
/// per-chunk report layout). Override via `--cycles` if the spec author
/// keeps the two files in non-standard locations.
#[derive(Debug, Clone, ClapArgs)]
pub struct GateCommonArgs {
    /// Path to `dependency_graph.json` (debundler analysis output).
    #[arg(
        long = "graph",
        env = "DEBUNDLE_GRAPH",
        required_unless_present = "cycles_path"
    )]
    pub dependency_graph_path: Option<PathBuf>,

    /// Override the default `cycles.json` location. Defaults to the
    /// sibling of `--graph`.
    #[arg(long = "cycles")]
    pub cycles_path: Option<PathBuf>,
}

impl GateCommonArgs {
    pub fn resolved_cycles_path(&self) -> Result<PathBuf> {
        if let Some(path) = &self.cycles_path {
            return Ok(path.clone());
        }
        let graph = self
            .dependency_graph_path
            .as_deref()
            .context("--graph or --cycles is required")?;
        Ok(graph
            .parent()
            .unwrap_or_else(|| Path::new("."))
            .join(output_layout::CYCLES_REPORT))
    }
}

#[derive(Debug, ClapArgs)]
pub struct GateListArgs {
    #[command(flatten)]
    pub common: GateCommonArgs,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<peel::OutputFormat>,
}

#[derive(Debug, ClapArgs)]
pub struct GateDescribeArgs {
    /// Blocking-SCC id (zero-based index into `cycles.json`).
    #[arg(requires = "dependency_graph_path")]
    pub id: usize,

    #[command(flatten)]
    pub common: GateCommonArgs,

    /// Restrict the recomputed evidence to edges that touch this
    /// binding (either source or target). Useful for narrowing a
    /// 1000-module SCC down to one symbol's contribution.
    #[arg(long = "binding")]
    pub binding: Option<String>,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<peel::OutputFormat>,
}

#[derive(Debug, ClapArgs)]
pub struct GateCutArgs {
    /// Blocking-SCC id (zero-based index into `cycles.json`).
    pub id: usize,

    #[command(flatten)]
    pub common: GateCommonArgs,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<peel::OutputFormat>,
}

#[derive(Debug, Clone, Serialize)]
pub struct GateListEntry {
    pub id: usize,
    pub module_count: usize,
    pub cut_count: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct GateListReport {
    pub blocking_sccs: Vec<GateListEntry>,
}

#[derive(Debug, Clone, Serialize)]
pub struct GateDescribeReport {
    pub id: usize,
    pub modules: Vec<ModulePath>,
    pub cut: Vec<CycleEdge>,
    /// Every constraining cross-module edge inside the SCC,
    /// recomputed from `dependency_graph.json` + `modules` (the wire
    /// entry carries only `modules` + `cut`).
    pub evidence: Vec<CycleEdge>,
}

#[derive(Debug, Clone, Serialize)]
pub struct GateCutReport {
    pub id: usize,
    pub cut: Vec<CycleEdge>,
}

pub fn run_gate_cli(args: GateArgs) -> Result<()> {
    match args.command {
        GateCommand::List(a) => run_list(a),
        GateCommand::Describe(a) => run_describe(a),
        GateCommand::Cut(a) => run_cut(a),
    }
}

fn load_cycles(common: &GateCommonArgs) -> Result<Vec<BlockingSccEntry>> {
    let path = common.resolved_cycles_path()?;
    // The pipeline writes `cycles.json` only when the gate rejects, so
    // its absence is the clean state: zero blocking SCCs. Returning an
    // empty list (rather than a read error) makes `gate list` on a
    // realizable spec print `[]` / "0 blocking SCC(s)" and exit 0,
    // distinguishable from a present-but-malformed file (which still
    // errors below). `gate describe` / `gate cut` then report
    // "no blocking SCC with id N (found 0 entries)" for any id.
    if !path.exists() {
        return Ok(Vec::new());
    }
    let text =
        std::fs::read_to_string(&path).with_context(|| format!("reading {}", path.display()))?;
    serde_json::from_str(&text)
        .with_context(|| format!("parsing blocking-SCC report {}", path.display()))
}

fn run_list(args: GateListArgs) -> Result<()> {
    let entries = load_cycles(&args.common)?;
    let report = GateListReport {
        blocking_sccs: entries
            .iter()
            .map(|e| GateListEntry {
                id: e.id,
                module_count: e.modules.len(),
                cut_count: e.cut.len(),
            })
            .collect(),
    };
    let format = peel::OutputFormat::resolve(args.format);
    if peel::print_ndjson_list(&report.blocking_sccs, format)? {
        return Ok(());
    }
    peel::print_report(&report, format, render_list_text).context("writing gate list output")
}

fn render_list_text(report: &GateListReport, out: &mut String) {
    out.push_str(&format!("{} blocking SCC(s)\n", report.blocking_sccs.len()));
    for entry in &report.blocking_sccs {
        out.push_str(&format!(
            "  {}  modules={}  cut={}\n",
            entry.id, entry.module_count, entry.cut_count,
        ));
    }
}

fn find_entry(entries: &[BlockingSccEntry], id: usize) -> Result<&BlockingSccEntry> {
    entries.iter().find(|e| e.id == id).ok_or_else(|| {
        anyhow::anyhow!(
            "no blocking SCC with id {id} in cycles.json (found {} entries)",
            entries.len(),
        )
    })
}

fn run_describe(args: GateDescribeArgs) -> Result<()> {
    let entries = load_cycles(&args.common)?;
    let entry = find_entry(&entries, args.id)?;
    let graph = crate::load_dependency_graph_report(
        args.common
            .dependency_graph_path
            .as_deref()
            .context("gate describe requires --graph")?,
    )?;

    let mut evidence = recompute_evidence(&graph, &entry.modules)?;
    if let Some(binding) = &args.binding {
        evidence.retain(|e| edge_touches_binding(e, binding));
    }

    let report = GateDescribeReport {
        id: entry.id,
        modules: entry.modules.clone(),
        cut: entry.cut.clone(),
        evidence,
    };
    let format = peel::OutputFormat::resolve(args.format);
    peel::print_report(&report, format, render_describe_text)
        .context("writing gate describe output")
}

fn render_describe_text(report: &GateDescribeReport, out: &mut String) {
    out.push_str(&format!(
        "blocking SCC #{}: {} module(s), cut of {}, {} evidence edge(s).\n",
        report.id,
        report.modules.len(),
        report.cut.len(),
        report.evidence.len(),
    ));

    if !report.modules.is_empty() {
        out.push_str("  modules:\n");
        for m in &report.modules {
            out.push_str(&format!("    {m}\n"));
        }
    }

    if !report.cut.is_empty() {
        out.push_str("  cut (actionable; break any of these by co-locating the binding pair):\n");
        for edge in &report.cut {
            render_edge(edge, out);
        }
    }

    if !report.evidence.is_empty() {
        // Group evidence by binding-pair blame key, same shape as
        // `render_cycle_summary`'s stderr block. Anonymous endpoints
        // fall back to `<anon stmt #ord>` / `<side-effect>` so every
        // row has a stable label.
        let mut groups: BTreeMap<BlamePairKey, BlamePairAgg> = BTreeMap::new();
        for edge in &report.evidence {
            let key = BlamePairKey::of(edge);
            let agg = groups.entry(key).or_insert(BlamePairAgg {
                kind: edge.kind,
                count: 0,
            });
            agg.count += 1;
        }
        let mut ranked: Vec<(BlamePairKey, BlamePairAgg)> = groups.into_iter().collect();
        ranked.sort_by(|a, b| b.1.count.cmp(&a.1.count).then(a.0.cmp(&b.0)));

        out.push_str("  evidence (grouped by binding pair):\n");
        for (key, agg) in &ranked {
            out.push_str(&format!(
                "    {n:>4}x  {fb} ({fm})  --{k}-->  {tb} ({tm})\n",
                n = agg.count,
                fb = key.from_label,
                fm = key.from,
                k = agg.kind.diagnostic_label(),
                tb = key.to_label,
                tm = key.to,
            ));
        }
    }
}

fn run_cut(args: GateCutArgs) -> Result<()> {
    let entries = load_cycles(&args.common)?;
    let entry = find_entry(&entries, args.id)?;
    let report = GateCutReport {
        id: entry.id,
        cut: entry.cut.clone(),
    };
    let format = peel::OutputFormat::resolve(args.format);
    if peel::print_ndjson_list(&report.cut, format)? {
        return Ok(());
    }
    peel::print_report(&report, format, render_cut_text).context("writing gate cut output")
}

fn render_cut_text(report: &GateCutReport, out: &mut String) {
    out.push_str(&format!(
        "blocking SCC #{}: cut of {}\n",
        report.id,
        report.cut.len(),
    ));
    for edge in &report.cut {
        render_edge(edge, out);
    }
}

fn render_edge(edge: &CycleEdge, out: &mut String) {
    let from_b = match &edge.from_binding {
        Some(a) => a.as_ref().to_string(),
        None => format!("<anon stmt #{}>", edge.statement_ordinal.0),
    };
    let to_b = match &edge.binding {
        Some(a) => a.as_ref().to_string(),
        None => "<side-effect>".to_string(),
    };
    out.push_str(&format!(
        "    {from_b} ({fm})  --{k}-->  {to_b} ({tm})  [stmt #{ord}]\n",
        fm = edge.from,
        k = edge.kind.diagnostic_label(),
        tm = edge.to,
        ord = edge.statement_ordinal.0,
    ));
    if let Some(cause) = &edge.sequenced_source_unit
        && let Purity::NotPure { reasons } = &cause.purity
    {
        for reason in reasons {
            let binding = if cause.binding_names.is_empty() {
                cause.source_unit_id.clone()
            } else {
                cause
                    .binding_names
                    .iter()
                    .map(Atom::as_ref)
                    .collect::<Vec<_>>()
                    .join(", ")
            };
            let location = reason
                .source_location
                .as_ref()
                .or(cause.source_location.as_ref())
                .map(|loc| {
                    format!(
                        "{}:{}:{}",
                        loc.source_path,
                        loc.start_line,
                        loc.start_column
                            .map_or_else(|| "?".to_string(), |column| column.to_string())
                    )
                })
                .unwrap_or_else(|| "<location unavailable>".to_string());
            out.push_str(&format!(
                "      impure initializer `{binding}` at {location}: {}{}\n",
                reason.rule.as_str(),
                reason
                    .detail
                    .as_deref()
                    .map(|detail| format!(" ({detail})"))
                    .unwrap_or_default()
            ));
            if let Some(guidance) = &reason.author_guidance {
                out.push_str(&format!("        {guidance}\n"));
            }
        }
    }
}

fn edge_touches_binding(edge: &CycleEdge, binding: &str) -> bool {
    edge.binding.as_ref().map(|a| a.as_ref()) == Some(binding)
        || edge.from_binding.as_ref().map(|a| a.as_ref()) == Some(binding)
}

/// Reconstruct diagnostic context for an SCC from `dependency_graph.json`.
/// Unlike gate validation, this includes lazy edges as well as constraining
/// edges. Preserve the report projection: omit intra-module/outside-SCC edges
/// and promoted edges whose callee is in another module; dedup sequencing by
/// module pair. This is diagnostic context, not a lossless reconstruction of
/// gate provenance; do not replace it with the gate's constraining-edge set.
///
/// Source labels prefer the first declared binding at the edge's statement
/// ordinal, falling back to the source source unit's first binding. Anonymous
/// statements retain `None` and render as `<anon stmt #N>`.
///
/// `cycles.json` uses canonical [`ModulePath`]s; resolve each source unit's interned
/// destination [`ModuleKey`] through the report's module table. A missing table
/// entry is malformed and errors; edges with absent source units are skipped.
fn recompute_evidence(
    graph: &DependencyGraphReport,
    modules: &[ModulePath],
) -> Result<Vec<CycleEdge>> {
    // Module table: interned key -> canonical path.
    let path_by_key: HashMap<&ModuleKey, &ModulePath> = graph
        .quotient
        .nodes
        .iter()
        .map(|entry| (&entry.key, &entry.path))
        .collect();
    // SourceUnit id -> report node and canonical destination. Reuse this index for
    // source labels and sequencing causes instead of scanning nodes per edge.
    let source_units: HashMap<&str, _> = graph
        .nodes
        .iter()
        .map(|n| {
            let path = path_by_key.get(&n.destination).copied().ok_or_else(|| {
                anyhow::anyhow!(
                    "source unit {} has destination {} with no module-table entry",
                    n.id,
                    n.destination
                )
            })?;
            Ok((n.id.as_str(), (n, path)))
        })
        .collect::<Result<_>>()?;
    // Statement ordinal -> first declared binding of any source unit
    // declaring at that ordinal. The materializer indexes by ordinal
    // (not source unit) when labeling the source side; we match that here.
    let from_binding_by_ordinal: HashMap<StatementOrdinal, Atom> = graph
        .nodes
        .iter()
        .filter_map(|n| {
            n.declared_bindings
                .first()
                .map(|b| (n.statement_ordinal, b.binding.clone()))
        })
        .collect();

    let scc_modules: BTreeSet<&ModulePath> = modules.iter().collect();

    let mut out = Vec::new();
    let mut seen_sequenced_pairs: BTreeSet<(&ModulePath, &ModulePath)> = BTreeSet::new();
    for edge in &graph.edges {
        let Some(&(source, from_mod)) = source_units.get(edge.source.as_str()) else {
            continue;
        };
        let Some(&(_, to_mod)) = source_units.get(edge.target.as_str()) else {
            continue;
        };
        if from_mod == to_mod {
            // Intra-module edges cannot explain a module-quotient cycle.
            continue;
        }
        if !scc_modules.contains(from_mod) || !scc_modules.contains(to_mod) {
            continue;
        }
        // Preserve the diagnostic projection's lenient promotion policy:
        // cross-module callees are represented by the direct caller->callee
        // edge, not the manufactured caller->target edge.
        if let Some(EdgeRoleReport::PromotedAtInit { callee_source_unit }) = &edge.role
            && let Some(&(_, callee_mod)) = source_units.get(callee_source_unit.as_str())
            && callee_mod != from_mod
        {
            continue;
        }
        if matches!(edge.edge_kind, DepKind::Sequenced) {
            // Mirror `chunk_constraining_module_edges`'s sequenced-edge
            // dedup: collapse parallel sequenced edges between the same
            // module pair into one evidence row.
            if !seen_sequenced_pairs.insert((from_mod, to_mod)) {
                continue;
            }
        }
        let from_binding = from_binding_by_ordinal
            .get(&edge.statement_ordinal)
            .cloned()
            .or_else(|| source.declared_bindings.first().map(|b| b.binding.clone()));
        out.push(CycleEdge {
            from: from_mod.clone(),
            to: to_mod.clone(),
            statement_ordinal: edge.statement_ordinal,
            binding: edge.binding.clone(),
            from_binding,
            kind: edge.edge_kind,
            sequenced_source_unit: (edge.edge_kind == DepKind::Sequenced
                && matches!(&source.purity, Purity::NotPure { .. }))
            .then(|| SequencedSourceUnitCause {
                source_unit_id: source.id.clone(),
                binding_names: source
                    .declared_bindings
                    .iter()
                    .map(|binding| binding.binding.clone())
                    .collect(),
                source_location: source.source_location.clone(),
                purity: source.purity.clone(),
            }),
        });
    }
    CycleEdge::sort_for_report(&mut out);
    Ok(out)
}

#[derive(Debug, Clone, Eq, PartialEq, Hash, Ord, PartialOrd)]
struct BlamePairKey {
    from_label: String,
    from: ModulePath,
    to: ModulePath,
    to_label: String,
}

impl BlamePairKey {
    fn of(edge: &CycleEdge) -> Self {
        let from_label = match &edge.from_binding {
            Some(a) => a.as_ref().to_string(),
            None => format!("<anon stmt #{}>", edge.statement_ordinal.0),
        };
        let to_label = match &edge.binding {
            Some(a) => a.as_ref().to_string(),
            None => "<side-effect>".to_string(),
        };
        Self {
            from_label,
            from: edge.from.clone(),
            to: edge.to.clone(),
            to_label,
        }
    }
}

#[derive(Debug, Clone, Copy)]
struct BlamePairAgg {
    kind: DepKind,
    count: usize,
}
