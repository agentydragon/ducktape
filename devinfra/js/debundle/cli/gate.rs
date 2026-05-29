//! `debundle gate` — query the realizability gate's rejected SCCs.
//!
//! When `materialize_logical_modules` rejects a spec, it writes one
//! entry per blocking SCC to `reports/tree/<chunk>/cycles.json` and
//! exits with a stderr summary. The trimmed wire shape (see
//! [`analysis::BlockingSccEntry`] and `WIRE_FORMAT.md`) carries
//! `id` / `modules` / `cut` per SCC — enough to dispatch follow-up
//! queries without storing the full evidence block on disk.
//!
//! This CLI surfaces three read-only views over that data:
//!
//! - `gate list` — one line per blocking SCC (id, modules count, cut size).
//! - `gate describe <id>` — full picture: modules list, cut, and
//!   **recomputed evidence**, sourced from `owner_graph.json` plus
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

use analysis::{
    BlockingSccEntry, CycleEdge, DepKind, EdgeRoleReport, OwnerGraphReport, StatementOrdinal,
};
use anyhow::{Context, Result};
use clap::{Args as ClapArgs, Subcommand};
use serde::Serialize;
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
    List(GateListArgs),
    /// Full picture for one blocking SCC: modules, cut, recomputed evidence.
    Describe(GateDescribeArgs),
    /// Just the cut edges for one blocking SCC. The actionable subset.
    Cut(GateIdArgs),
}

/// Shared args: paths to `owner_graph.json` (always written; carries
/// the recomputable per-edge evidence) and `cycles.json` (the trimmed
/// blocking-SCC wire shape).
///
/// `cycles.json` defaults to the sibling of `--graph` (the standard
/// per-chunk report layout). Override via `--cycles` if the spec author
/// keeps the two files in non-standard locations.
#[derive(Debug, Clone, ClapArgs)]
pub struct GateCommonArgs {
    /// Path to `owner_graph.json` (debundler analysis output).
    #[arg(long = "graph", env = "DEBUNDLE_GRAPH")]
    pub owner_graph_path: PathBuf,

    /// Per-module YAML tree root. Unused today; kept here so every
    /// `debundle ...` command shares the same env/flag triple
    /// (`--graph`/`--modules`/`--source-root`) per docs/cli.md.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    pub modules_root: PathBuf,

    /// Override the default `cycles.json` location. Defaults to the
    /// sibling of `--graph`.
    #[arg(long = "cycles")]
    pub cycles_path: Option<PathBuf>,
}

impl GateCommonArgs {
    pub fn resolved_cycles_path(&self) -> PathBuf {
        if let Some(p) = &self.cycles_path {
            return p.clone();
        }
        let parent = self
            .owner_graph_path
            .parent()
            .unwrap_or_else(|| Path::new("."));
        parent.join(output_layout::CYCLES_REPORT)
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
pub struct GateIdArgs {
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
    pub modules: Vec<String>,
    pub cut: Vec<CycleEdge>,
    /// Recomputed from `owner_graph.json` + `modules`. Same shape
    /// as the in-memory `CycleReport.evidence` the gate emitted to
    /// stderr at rejection time.
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
    let path = common.resolved_cycles_path();
    let text =
        std::fs::read_to_string(&path).with_context(|| format!("reading {}", path.display()))?;
    serde_json::from_str(&text)
        .with_context(|| format!("parsing blocking-SCC report {}", path.display()))
}

fn load_graph(common: &GateCommonArgs) -> Result<OwnerGraphReport> {
    let text = std::fs::read_to_string(&common.owner_graph_path)
        .with_context(|| format!("reading {}", common.owner_graph_path.display()))?;
    serde_json::from_str(&text)
        .with_context(|| format!("parsing owner graph {}", common.owner_graph_path.display()))
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
    if format == peel::OutputFormat::Ndjson {
        for entry in &report.blocking_sccs {
            println!("{}", serde_json::to_string(entry)?);
        }
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
    let graph = load_graph(&args.common)?;

    let mut evidence = recompute_evidence(&graph, &entry.modules);
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
                k = dep_kind_short(agg.kind),
                tb = key.to_label,
                tm = key.to,
            ));
        }
    }
}

fn run_cut(args: GateIdArgs) -> Result<()> {
    let entries = load_cycles(&args.common)?;
    let entry = find_entry(&entries, args.id)?;
    let report = GateCutReport {
        id: entry.id,
        cut: entry.cut.clone(),
    };
    let format = peel::OutputFormat::resolve(args.format);
    if format == peel::OutputFormat::Ndjson {
        for edge in &report.cut {
            println!("{}", serde_json::to_string(edge)?);
        }
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
        k = dep_kind_short(edge.kind),
        tm = edge.to,
        ord = edge.statement_ordinal.0,
    ));
}

fn dep_kind_short(kind: DepKind) -> &'static str {
    match kind {
        DepKind::EagerUse => "at-init",
        DepKind::LazyUse => "lazy",
        DepKind::EagerRebind => "at-init rebind",
        DepKind::LazyRebind => "lazy rebind",
        DepKind::Sequenced => "side-effect",
        DepKind::LocalEffect => "local-effect",
    }
}

fn edge_touches_binding(edge: &CycleEdge, binding: &str) -> bool {
    edge.binding.as_ref().map(|a| a.as_ref()) == Some(binding)
        || edge.from_binding.as_ref().map(|a| a.as_ref()) == Some(binding)
}

/// Recompute the `evidence` block for a blocking SCC from
/// `owner_graph.json`. The on-disk `cycles.json` no longer carries
/// it — it's recoverable by walking every edge in the owner graph
/// and keeping those whose source and target both map to a module
/// in the SCC's `modules` set.
///
/// Output mirrors the materializer's
/// [`analysis::CycleReport`]`.evidence`: one `CycleEdge` per owner
/// edge whose endpoints both fall in different modules of the SCC,
/// with `from_binding` set to the first declared binding of any
/// owner declaring at the same statement ordinal (anonymous source
/// statements remain `None` and render as `<anon stmt #N>`), and
/// `binding` set to the edge's target binding.
///
/// Filters applied to mirror the in-memory build:
///
/// * **Drop intra-module owner edges** (`from == to` after projection).
///   `ModuleQuotient::record_reason` returns early on these; the
///   materializer's evidence iteration is over the quotient.
/// * **Drop cross-module `PromotedAtInit` edges whose callee
///   module differs from the caller**. `EndpointView::Lenient`
///   (used by `build_module_quotient`) treats them as redundant
///   with the already-recorded `R -> callee` edge.
/// * **Dedup sequenced edges per `(from_module, to_module)` pair** —
///   `record_reason` collapses parallel sequenced reasons into one
///   constraint.
///
/// The reconstruction is **approximate**: the on-wire owner graph
/// drops a few sub-edge attributes the in-memory `EdgeReason`
/// carries (e.g. some at-init promotion details), so the recomputed
/// evidence count may differ by a small number of rows from the
/// pre-trim value. The per-binding-pair blame view (the surface
/// spec authors actually read) is unaffected.
fn recompute_evidence(graph: &OwnerGraphReport, modules: &[String]) -> Vec<CycleEdge> {
    // Owner id -> destination module key. The cycles.json `modules`
    // entries are precisely these interned module keys.
    let owner_module: HashMap<&str, &str> = graph
        .nodes
        .iter()
        .map(|n| (n.id.as_str(), n.destination.as_str()))
        .collect();
    // Owner id -> first declared binding (matches the heuristic
    // `from_binding_by_ordinal` builds in validation.rs).
    let owner_first_binding: HashMap<&str, Atom> = graph
        .nodes
        .iter()
        .filter_map(|n| {
            n.declared_bindings
                .first()
                .map(|b| (n.id.as_str(), b.binding.clone()))
        })
        .collect();
    // Statement ordinal -> first declared binding of any owner
    // declaring at that ordinal. The materializer indexes by ordinal
    // (not owner) when labeling the source side; we match that here.
    let from_binding_by_ordinal: HashMap<StatementOrdinal, Atom> = graph
        .nodes
        .iter()
        .filter_map(|n| {
            n.declared_bindings
                .first()
                .map(|b| (n.statement_ordinal, b.binding.clone()))
        })
        .collect();

    let scc_modules: BTreeSet<&str> = modules.iter().map(|s| s.as_str()).collect();

    let mut out = Vec::new();
    let mut seen_sequenced_pairs: BTreeSet<(&str, &str)> = BTreeSet::new();
    for edge in &graph.edges {
        let Some(&from_mod) = owner_module.get(edge.source.as_str()) else {
            continue;
        };
        let Some(&to_mod) = owner_module.get(edge.target.as_str()) else {
            continue;
        };
        if from_mod == to_mod {
            // Same-module owner edges never enter the quotient
            // (`ModuleQuotient::record_reason` returns early when
            // `from == to`). The materializer's evidence iteration
            // is over the quotient, so intra-module edges are not
            // evidence — match that here.
            continue;
        }
        if !scc_modules.contains(from_mod) || !scc_modules.contains(to_mod) {
            continue;
        }
        // `build_module_quotient` uses `EndpointView::Lenient`, which
        // drops cross-module `PromotedAtInit` edges whose callee
        // module differs from the caller — ESM DFS post-order makes
        // the manufactured `R -> target` redundant with the already-
        // recorded `R -> callee` edge (see graph.rs `partition_endpoints`).
        // Match that filter here so the recomputed evidence count
        // agrees with the pre-trim cycles.json output.
        if let Some(EdgeRoleReport::PromotedAtInit { callee_owner }) = &edge.role {
            if let Some(&callee_mod) = owner_module.get(callee_owner.as_str()) {
                if callee_mod != from_mod {
                    continue;
                }
            }
        }
        if matches!(edge.edge_kind, DepKind::Sequenced) {
            // Mirror `build_module_quotient`'s sequenced-edge dedup
            // (graph.rs `record_reason` site): collapse parallel
            // sequenced edges between the same module pair into one
            // evidence row.
            if !seen_sequenced_pairs.insert((from_mod, to_mod)) {
                continue;
            }
        }
        let from_binding = from_binding_by_ordinal
            .get(&edge.statement_ordinal)
            .cloned()
            .or_else(|| owner_first_binding.get(edge.source.as_str()).cloned());
        out.push(CycleEdge {
            from: from_mod.to_string(),
            to: to_mod.to_string(),
            statement_ordinal: edge.statement_ordinal,
            binding: edge.binding.clone(),
            from_binding,
            kind: edge.edge_kind,
        });
    }
    out.sort_by(|a, b| {
        (
            a.from.as_str(),
            a.to.as_str(),
            a.statement_ordinal,
            &a.binding,
            a.kind,
        )
            .cmp(&(
                b.from.as_str(),
                b.to.as_str(),
                b.statement_ordinal,
                &b.binding,
                b.kind,
            ))
    });
    out
}

#[derive(Debug, Clone, Eq, PartialEq, Hash, Ord, PartialOrd)]
struct BlamePairKey {
    from_label: String,
    from: String,
    to: String,
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

#[cfg(test)]
mod tests {
    use super::*;
    use analysis::{AtomicGraphReport, Purity};
    use analysis::{
        BindingReport, EdgeRoleReport, OwnerGraphEdgeReport, OwnerGraphNodeReport,
        OwnerGraphQuotientReport, OwnerGraphReport, StatementKind,
    };
    use report_fixtures::module_ref;

    fn owner_node(
        id: &str,
        statement_ordinal: usize,
        bindings: &[&str],
        module_label: &str,
    ) -> OwnerGraphNodeReport {
        // The destination key string is the module label so
        // `recompute_evidence`'s `owner_module` lookup matches the SCC
        // `modules` entries (which are these same keys).
        OwnerGraphNodeReport {
            id: id.to_string(),
            statement_ordinal: StatementOrdinal(statement_ordinal),
            source_location: None,
            declared_bindings: bindings
                .iter()
                .map(|b| BindingReport {
                    binding: Atom::from(*b),
                    export_name: Atom::from(*b),
                })
                .collect(),
            statement_kind: StatementKind::VarDecl,
            purity: Purity::Pure,
            destination: module_ref(module_label, false),
        }
    }

    fn owner_edge(
        id: &str,
        source: &str,
        target: &str,
        kind: DepKind,
        binding: Option<&str>,
        ord: usize,
    ) -> OwnerGraphEdgeReport {
        OwnerGraphEdgeReport {
            id: id.to_string(),
            source: source.to_string(),
            target: target.to_string(),
            edge_kind: kind,
            binding: binding.map(Atom::from),
            statement_ordinal: StatementOrdinal(ord),
            constrains_init_order: matches!(kind, DepKind::EagerUse | DepKind::Sequenced),
            role: None::<EdgeRoleReport>,
        }
    }

    fn empty_quotient() -> OwnerGraphQuotientReport {
        OwnerGraphQuotientReport {
            nodes: Vec::new(),
            edges: Vec::new(),
            sccs: Vec::new(),
        }
    }

    fn empty_atomic_graph() -> AtomicGraphReport {
        AtomicGraphReport {
            nodes: Vec::new(),
            edges: Vec::new(),
        }
    }

    #[test]
    fn recompute_evidence_includes_cross_module_edges_in_scc() {
        let graph = OwnerGraphReport {
            chunk_id: "test".to_string(),
            nodes: vec![
                owner_node("owner:0", 0, &["a"], "mod_a"),
                owner_node("owner:1", 1, &["b"], "mod_b"),
                owner_node("owner:2", 2, &["c"], "mod_c"),
            ],
            edges: vec![
                // mod_a -> mod_b (in SCC)
                owner_edge("e0", "owner:0", "owner:1", DepKind::EagerUse, Some("b"), 0),
                // mod_b -> mod_a (in SCC, completes cycle)
                owner_edge("e1", "owner:1", "owner:0", DepKind::EagerUse, Some("a"), 1),
                // mod_a -> mod_c (NOT in SCC — should be filtered out)
                owner_edge("e2", "owner:0", "owner:2", DepKind::EagerUse, Some("c"), 0),
            ],
            quotient: empty_quotient(),
            atomic_graph: empty_atomic_graph(),
        };
        let evidence =
            super::recompute_evidence(&graph, &["mod_a".to_string(), "mod_b".to_string()]);
        assert_eq!(evidence.len(), 2, "{evidence:#?}");
        assert!(
            evidence
                .iter()
                .any(|e| e.from == "mod_a" && e.to == "mod_b")
        );
        assert!(
            evidence
                .iter()
                .any(|e| e.from == "mod_b" && e.to == "mod_a")
        );
        // Source binding labels come from the source owner's first
        // declared binding.
        for e in &evidence {
            assert!(e.from_binding.is_some(), "{e:#?}");
        }
    }

    #[test]
    fn recompute_evidence_skips_intra_module_edges() {
        let graph = OwnerGraphReport {
            chunk_id: "test".to_string(),
            nodes: vec![
                owner_node("owner:0", 0, &["a"], "mod_a"),
                owner_node("owner:1", 1, &["b"], "mod_a"), // same module
            ],
            edges: vec![owner_edge(
                "e0",
                "owner:0",
                "owner:1",
                DepKind::EagerUse,
                Some("b"),
                0,
            )],
            quotient: empty_quotient(),
            atomic_graph: empty_atomic_graph(),
        };
        let evidence = super::recompute_evidence(&graph, &["mod_a".to_string()]);
        assert!(evidence.is_empty(), "{evidence:#?}");
    }

    #[test]
    fn recompute_evidence_includes_lazy_edges() {
        // Evidence is the full constraining-and-non-constraining edge
        // set inside the SCC (unlike `cut`, which is constraining-only).
        let graph = OwnerGraphReport {
            chunk_id: "test".to_string(),
            nodes: vec![
                owner_node("owner:0", 0, &["a"], "mod_a"),
                owner_node("owner:1", 1, &["b"], "mod_b"),
            ],
            edges: vec![owner_edge(
                "e0",
                "owner:0",
                "owner:1",
                DepKind::LazyUse,
                Some("b"),
                0,
            )],
            quotient: empty_quotient(),
            atomic_graph: empty_atomic_graph(),
        };
        let evidence =
            super::recompute_evidence(&graph, &["mod_a".to_string(), "mod_b".to_string()]);
        assert_eq!(evidence.len(), 1);
        assert_eq!(evidence[0].kind, DepKind::LazyUse);
    }

    #[test]
    fn cycles_path_defaults_to_graph_sibling() {
        let common = GateCommonArgs {
            owner_graph_path: PathBuf::from("/tmp/reports/static/app/owner_graph.json"),
            modules_root: PathBuf::from("/tmp/modules"),
            cycles_path: None,
        };
        assert_eq!(
            common.resolved_cycles_path(),
            PathBuf::from("/tmp/reports/static/app/cycles.json")
        );
    }

    #[test]
    fn cycles_path_override_wins() {
        let common = GateCommonArgs {
            owner_graph_path: PathBuf::from("/tmp/reports/static/app/owner_graph.json"),
            modules_root: PathBuf::from("/tmp/modules"),
            cycles_path: Some(PathBuf::from("/other/cycles.json")),
        };
        assert_eq!(
            common.resolved_cycles_path(),
            PathBuf::from("/other/cycles.json")
        );
    }

    #[test]
    fn edge_touches_binding_matches_either_endpoint() {
        let edge = CycleEdge {
            from: "mod_a".to_string(),
            to: "mod_b".to_string(),
            statement_ordinal: StatementOrdinal(0),
            binding: Some(Atom::from("target")),
            from_binding: Some(Atom::from("source")),
            kind: DepKind::EagerUse,
        };
        assert!(super::edge_touches_binding(&edge, "source"));
        assert!(super::edge_touches_binding(&edge, "target"));
        assert!(!super::edge_touches_binding(&edge, "unrelated"));
    }
}
