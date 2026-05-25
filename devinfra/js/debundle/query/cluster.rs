//! `debundle cluster` — list 1-hop quotient neighbors of a module
//! or binding.
//!
//! The cluster of a module M is every module N such that the
//! module-quotient graph has an edge `M -> N` or `N -> M`. Useful
//! for asking "what else lives close to this binding?" before
//! deciding where to assign a residual symbol.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::PathBuf;

use anyhow::{Context, Result, bail};
use clap::Args;
use serde::Serialize;

use analysis::OwnerGraphReport;
use spec_modules::{collect_module_files, module_path_from_file, read_module_file};

use super::io::{OutputFormat, print_records};

#[derive(Debug, Clone, Args)]
pub struct ClusterArgs {
    /// Path to `owner_graph.json` (debundler analysis output).
    #[arg(long = "graph")]
    pub owner_graph_path: PathBuf,

    /// Root of emitted-module `*.yaml` spec files. Required only when
    /// `--binding` is used.
    #[arg(long = "modules")]
    pub modules_root: Option<PathBuf>,

    /// Module path to query (e.g. `runtime/plugins`).
    #[arg(long = "module", conflicts_with = "binding")]
    pub module: Option<String>,

    /// Binding name to query. Cluster is the neighbors of the
    /// module that owns this binding. Requires `--modules`.
    #[arg(long = "binding", conflicts_with = "module")]
    pub binding: Option<String>,

    #[command(flatten)]
    pub output: OutputFormat,
}

#[derive(Debug, Clone, Serialize)]
pub struct ClusterRecord {
    pub neighbor: String,
    pub label: String,
    /// `inbound`: edges flow from neighbor into the query module.
    /// `outbound`: edges flow from the query module into neighbor.
    /// `both`: at least one edge in each direction.
    pub direction: Direction,
    pub edge_count: usize,
    pub constrains_init_order: bool,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Direction {
    Inbound,
    Outbound,
    Both,
}

pub fn run(args: ClusterArgs) -> Result<()> {
    let records = collect(&args)?;
    print_records(&records, args.output)
}

pub fn collect(args: &ClusterArgs) -> Result<Vec<ClusterRecord>> {
    if args.binding.is_none() && args.module.is_none() {
        bail!("pass --module <path> or --binding <name>");
    }
    let graph: OwnerGraphReport = serde_json::from_str(
        &fs::read_to_string(&args.owner_graph_path)
            .with_context(|| format!("reading {}", args.owner_graph_path.display()))?,
    )
    .with_context(|| format!("parsing {}", args.owner_graph_path.display()))?;

    let target = resolve_query_module(args, &graph)?;
    let label_by_id: BTreeMap<String, String> = graph
        .quotient
        .nodes
        .iter()
        .map(|node| (node.id.clone(), node.label.clone()))
        .collect();

    let mut inbound_counts: BTreeMap<String, (usize, bool)> = BTreeMap::new();
    let mut outbound_counts: BTreeMap<String, (usize, bool)> = BTreeMap::new();
    for edge in &graph.quotient.edges {
        if matches_module(&edge.target, &target) && !matches_module(&edge.source, &target) {
            let entry = inbound_counts.entry(edge.source.clone()).or_default();
            entry.0 += 1;
            entry.1 |= edge.constrains_init_order;
        }
        if matches_module(&edge.source, &target) && !matches_module(&edge.target, &target) {
            let entry = outbound_counts.entry(edge.target.clone()).or_default();
            entry.0 += 1;
            entry.1 |= edge.constrains_init_order;
        }
    }

    let mut neighbors: BTreeSet<String> = inbound_counts.keys().cloned().collect();
    neighbors.extend(outbound_counts.keys().cloned());
    let mut records: Vec<ClusterRecord> = neighbors
        .into_iter()
        .map(|neighbor| {
            let inbound = inbound_counts.get(&neighbor).copied();
            let outbound = outbound_counts.get(&neighbor).copied();
            let direction = match (inbound.is_some(), outbound.is_some()) {
                (true, true) => Direction::Both,
                (true, false) => Direction::Inbound,
                (false, true) => Direction::Outbound,
                (false, false) => unreachable!("neighbor came from at least one bucket"),
            };
            let edge_count = inbound.map_or(0, |(n, _)| n) + outbound.map_or(0, |(n, _)| n);
            let constrains_init_order =
                inbound.is_some_and(|(_, c)| c) || outbound.is_some_and(|(_, c)| c);
            let label = label_by_id
                .get(&neighbor)
                .cloned()
                .unwrap_or_else(|| neighbor.clone());
            ClusterRecord {
                neighbor,
                label,
                direction,
                edge_count,
                constrains_init_order,
            }
        })
        .collect();
    records.sort_by(|a, b| {
        b.edge_count
            .cmp(&a.edge_count)
            .then_with(|| a.neighbor.cmp(&b.neighbor))
    });
    Ok(records)
}

fn matches_module(scc_module: &str, target: &str) -> bool {
    if scc_module == target {
        return true;
    }
    scc_module.ends_with(&format!("::{target}"))
}

fn resolve_query_module(args: &ClusterArgs, graph: &OwnerGraphReport) -> Result<String> {
    if let Some(module) = &args.module {
        let any_match = graph
            .quotient
            .nodes
            .iter()
            .any(|node| matches_module(&node.id, module));
        if !any_match {
            bail!("module {module:?} does not appear in the quotient graph");
        }
        return Ok(module.clone());
    }
    let binding = args.binding.as_ref().expect("guarded above");
    let modules_root = args
        .modules_root
        .as_ref()
        .ok_or_else(|| anyhow::anyhow!("--binding requires --modules"))?;
    for path in collect_module_files(modules_root)? {
        let module_path = module_path_from_file(&path, modules_root);
        for member in read_module_file(&path)?.members {
            if member.selector.binding.name == *binding {
                return Ok(module_path);
            }
        }
    }
    bail!(
        "binding {binding:?} not found in any spec module under {}",
        modules_root.display()
    );
}

#[cfg(test)]
mod tests {
    use super::*;
    use analysis::{
        AtomicGraphReport, DepKind, ModuleReportRef, OwnerGraphQuotientReport, OwnerGraphReport,
        QuotientEdgeReport,
    };
    use tempfile::TempDir;

    fn module_ref(id: &str, residual: bool) -> ModuleReportRef {
        ModuleReportRef {
            id: id.to_string(),
            label: id.to_string(),
            residual,
            index: None,
            target_file: None,
        }
    }

    fn edge(id: &str, source: &str, target: &str, constrains: bool) -> QuotientEdgeReport {
        QuotientEdgeReport {
            id: id.to_string(),
            source: source.to_string(),
            target: target.to_string(),
            edge_kinds: vec![DepKind::EagerUse],
            constrains_init_order: constrains,
        }
    }

    fn graph(nodes: Vec<ModuleReportRef>, edges: Vec<QuotientEdgeReport>) -> OwnerGraphReport {
        OwnerGraphReport {
            chunk_id: "static/app".to_string(),
            nodes: Vec::new(),
            edges: Vec::new(),
            quotient: OwnerGraphQuotientReport {
                nodes,
                edges,
                sccs: Vec::new(),
            },
            atomic_graph: AtomicGraphReport {
                nodes: Vec::new(),
                edges: Vec::new(),
            },
        }
    }

    fn write_graph(dir: &TempDir, graph: &OwnerGraphReport) -> PathBuf {
        let path = dir.path().join("owner_graph.json");
        fs::write(&path, serde_json::to_string(graph).unwrap()).unwrap();
        path
    }

    fn base_args(graph_path: PathBuf) -> ClusterArgs {
        ClusterArgs {
            owner_graph_path: graph_path,
            modules_root: None,
            module: None,
            binding: None,
            output: OutputFormat::default(),
        }
    }

    #[test]
    fn cluster_reports_inbound_and_outbound_neighbors() {
        let dir = TempDir::new().unwrap();
        let g = graph(
            vec![
                module_ref("static/app::runtime/plugins", false),
                module_ref("static/app::runtime/a", false),
                module_ref("static/app::runtime/b", false),
                module_ref("static/app::runtime/c", false),
            ],
            vec![
                edge(
                    "qe:0",
                    "static/app::runtime/a",
                    "static/app::runtime/plugins",
                    true,
                ),
                edge(
                    "qe:1",
                    "static/app::runtime/plugins",
                    "static/app::runtime/b",
                    false,
                ),
                edge(
                    "qe:2",
                    "static/app::runtime/a",
                    "static/app::runtime/plugins",
                    false,
                ),
                edge(
                    "qe:3",
                    "static/app::runtime/plugins",
                    "static/app::runtime/c",
                    false,
                ),
                edge(
                    "qe:4",
                    "static/app::runtime/c",
                    "static/app::runtime/plugins",
                    false,
                ),
            ],
        );
        let graph_path = write_graph(&dir, &g);
        let mut args = base_args(graph_path);
        args.module = Some("runtime/plugins".to_string());
        let out = collect(&args).unwrap();
        assert_eq!(out.len(), 3);
        // c is `both` direction. Edge count is 2 (one each way).
        let c = out
            .iter()
            .find(|r| r.neighbor.ends_with("runtime/c"))
            .unwrap();
        assert_eq!(c.direction, Direction::Both);
        assert_eq!(c.edge_count, 2);
        // a is inbound only; count is 2 edges (qe:0, qe:2).
        let a = out
            .iter()
            .find(|r| r.neighbor.ends_with("runtime/a"))
            .unwrap();
        assert_eq!(a.direction, Direction::Inbound);
        assert_eq!(a.edge_count, 2);
        assert!(a.constrains_init_order);
        // b is outbound only.
        let b = out
            .iter()
            .find(|r| r.neighbor.ends_with("runtime/b"))
            .unwrap();
        assert_eq!(b.direction, Direction::Outbound);
    }

    #[test]
    fn cluster_errors_for_unknown_module() {
        let dir = TempDir::new().unwrap();
        let g = graph(Vec::new(), Vec::new());
        let graph_path = write_graph(&dir, &g);
        let mut args = base_args(graph_path);
        args.module = Some("runtime/plugins".to_string());
        assert!(collect(&args).is_err());
    }
}
