//! `debundle scc` — list strongly-connected components in the
//! module-quotient graph.
//!
//! Reads `owner_graph.json`'s `module_graph.sccs` directly. The
//! command is a thin projection: filters operate over the existing
//! fields, no new graph analysis happens here.
//!
//! Default output: one JSON document containing an array of SCC
//! records. With `--ndjson`, one SCC per line so the output can be
//! piped through `jq -c 'select(.size==1)'` and similar.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use clap::Args;
use serde::Serialize;

use analysis::{OwnerGraphReport, QuotientSccReport};
use spec_modules::{
    collect_module_files, is_residual_module_path, module_path_from_file, read_module_file,
};

use super::io::{OutputFormat, print_records};

#[derive(Debug, Clone, Args)]
pub struct SccArgs {
    /// Path to `owner_graph.json` (debundler analysis output).
    #[arg(long = "graph")]
    pub owner_graph_path: PathBuf,

    /// Root of emitted-module `*.yaml` spec files. Needed to resolve
    /// `--binding <name>` to a module path; otherwise optional.
    #[arg(long = "modules")]
    pub modules_root: Option<PathBuf>,

    /// Restrict output to the SCC that contains this module path
    /// (e.g. `runtime/plugins`).
    #[arg(long = "module")]
    pub module: Option<String>,

    /// Restrict output to the SCC that contains the module owning
    /// this binding. Requires `--modules`.
    #[arg(long = "binding")]
    pub binding: Option<String>,

    /// Drop SCCs smaller than `n` modules. `--min-size 2` selects
    /// only cycles.
    #[arg(long = "min-size", default_value_t = 0)]
    pub min_size: usize,

    /// Drop SCCs larger than `n` modules. Zero means unlimited.
    #[arg(long = "max-size", default_value_t = 0)]
    pub max_size: usize,

    /// Keep only SCCs whose every module is under a `residual/`
    /// path. Useful for finding clean extraction candidates that
    /// are not yet promoted out of the catch-all.
    #[arg(long = "residual-only", default_value_t = false)]
    pub residual_only: bool,

    /// Keep only cycles (`size >= 2 && is_cycle`).
    #[arg(long = "cycles-only", default_value_t = false)]
    pub cycles_only: bool,

    /// Keep only singletons (`size == 1`). Mutually exclusive with
    /// `--cycles-only`.
    #[arg(long = "singletons-only", default_value_t = false)]
    pub singletons_only: bool,

    #[command(flatten)]
    pub output: OutputFormat,
}

#[derive(Debug, Clone, Serialize)]
pub struct SccRecord {
    pub id: String,
    pub size: usize,
    pub is_cycle: bool,
    pub realizable: bool,
    pub all_residual: bool,
    pub modules: Vec<String>,
    pub labels: Vec<String>,
    pub module_edge_count: usize,
    pub constraining_module_edge_count: usize,
}

pub fn run(args: SccArgs) -> Result<()> {
    let records = collect(&args)?;
    print_records(&records, args.output)
}

pub fn collect(args: &SccArgs) -> Result<Vec<SccRecord>> {
    if args.cycles_only && args.singletons_only {
        bail!("--cycles-only and --singletons-only are mutually exclusive");
    }
    if args.binding.is_some() && args.modules_root.is_none() {
        bail!("--binding requires --modules to resolve the binding's module path");
    }
    let graph: OwnerGraphReport = serde_json::from_str(
        &fs::read_to_string(&args.owner_graph_path)
            .with_context(|| format!("reading {}", args.owner_graph_path.display()))?,
    )
    .with_context(|| format!("parsing {}", args.owner_graph_path.display()))?;

    let module_filter = resolve_module_filter(args)?;

    let mut out: Vec<SccRecord> = graph
        .quotient
        .sccs
        .iter()
        .filter(|scc| match args.min_size {
            0 => true,
            min => scc.modules.len() >= min,
        })
        .filter(|scc| match args.max_size {
            0 => true,
            max => scc.modules.len() <= max,
        })
        .filter(|scc| !args.cycles_only || (scc.modules.len() >= 2 && scc.is_cycle))
        .filter(|scc| !args.singletons_only || scc.modules.len() == 1)
        .filter(|scc| !args.residual_only || all_residual(scc))
        .filter(|scc| match &module_filter {
            None => true,
            Some(target) => scc.modules.iter().any(|m| matches_module(m, target)),
        })
        .map(record_from_scc)
        .collect();

    out.sort_by(|a, b| b.size.cmp(&a.size).then_with(|| a.id.cmp(&b.id)));
    Ok(out)
}

fn resolve_module_filter(args: &SccArgs) -> Result<Option<String>> {
    if let Some(module) = &args.module {
        return Ok(Some(module.clone()));
    }
    let Some(binding) = &args.binding else {
        return Ok(None);
    };
    let modules_root = args.modules_root.as_ref().expect("checked above");
    for path in collect_module_files(modules_root)? {
        let module_path = module_path_from_file(&path, modules_root);
        for member in read_module_file(&path)?.members {
            if member.selector.binding.name == *binding {
                return Ok(Some(module_path));
            }
        }
    }
    bail!(
        "binding {binding:?} not found in any spec module under {}",
        modules_root.display()
    );
}

fn matches_module(scc_module: &str, target: &str) -> bool {
    // Quotient module ids in the report carry their chunk-id prefix
    // (e.g. `static/app::runtime/plugins`). Match either the bare
    // module path or the suffix-anchored form so callers can pass
    // either.
    if scc_module == target {
        return true;
    }
    let needle = format!("::{target}");
    scc_module.ends_with(&needle)
}

fn all_residual(scc: &QuotientSccReport) -> bool {
    scc.modules.iter().all(|module| {
        let path = strip_chunk_prefix(module);
        is_residual_module_path(path)
    })
}

fn strip_chunk_prefix(module: &str) -> &str {
    module
        .rsplit_once("::")
        .map(|(_, path)| path)
        .unwrap_or(module)
}

fn record_from_scc(scc: &QuotientSccReport) -> SccRecord {
    SccRecord {
        id: scc.id.clone(),
        size: scc.modules.len(),
        is_cycle: scc.is_cycle,
        realizable: scc.realizable,
        all_residual: all_residual(scc),
        modules: scc.modules.clone(),
        labels: scc.labels.clone(),
        module_edge_count: scc.module_edge_ids.len(),
        constraining_module_edge_count: scc.constraining_module_edge_ids.len(),
    }
}

/// Mapping of binding name → module path under `modules_root`.
/// Surfaced as a helper because `cluster` reuses it. Not part of
/// `SccArgs`-flow; lives here because it's small.
pub fn build_binding_index(modules_root: &Path) -> Result<BTreeMap<String, String>> {
    let mut out = BTreeMap::new();
    for path in collect_module_files(modules_root)? {
        let module_path = module_path_from_file(&path, modules_root);
        for member in read_module_file(&path)?.members {
            out.insert(member.selector.binding.name, module_path.clone());
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use analysis::{
        AtomicGraphReport, ModuleReportRef, OwnerGraphQuotientReport, OwnerGraphReport,
    };
    use tempfile::TempDir;

    fn scc(id: &str, modules: &[&str], is_cycle: bool) -> QuotientSccReport {
        QuotientSccReport {
            id: id.to_string(),
            modules: modules.iter().map(|m| (*m).to_string()).collect(),
            labels: modules.iter().map(|m| (*m).to_string()).collect(),
            is_cycle,
            realizable: true,
            module_edge_ids: Vec::new(),
            constraining_module_edge_ids: Vec::new(),
        }
    }

    fn graph_with_sccs(sccs: Vec<QuotientSccReport>) -> OwnerGraphReport {
        OwnerGraphReport {
            chunk_id: "static/app".to_string(),
            nodes: Vec::new(),
            edges: Vec::new(),
            quotient: OwnerGraphQuotientReport {
                nodes: Vec::new(),
                edges: Vec::new(),
                sccs,
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

    fn base_args(graph_path: PathBuf) -> SccArgs {
        SccArgs {
            owner_graph_path: graph_path,
            modules_root: None,
            module: None,
            binding: None,
            min_size: 0,
            max_size: 0,
            residual_only: false,
            cycles_only: false,
            singletons_only: false,
            output: OutputFormat::default(),
        }
    }

    #[test]
    fn singletons_only_drops_cycles() {
        let dir = TempDir::new().unwrap();
        let graph = graph_with_sccs(vec![
            scc("scc:0", &["static/app::runtime/plugins"], false),
            scc(
                "scc:1",
                &["static/app::runtime/a", "static/app::runtime/b"],
                true,
            ),
        ]);
        let graph_path = write_graph(&dir, &graph);
        let mut args = base_args(graph_path);
        args.singletons_only = true;
        let out = collect(&args).unwrap();
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].id, "scc:0");
        assert_eq!(out[0].size, 1);
    }

    #[test]
    fn cycles_only_drops_singletons() {
        let dir = TempDir::new().unwrap();
        let graph = graph_with_sccs(vec![
            scc("scc:0", &["static/app::runtime/plugins"], false),
            scc(
                "scc:1",
                &["static/app::runtime/a", "static/app::runtime/b"],
                true,
            ),
        ]);
        let graph_path = write_graph(&dir, &graph);
        let mut args = base_args(graph_path);
        args.cycles_only = true;
        let out = collect(&args).unwrap();
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].id, "scc:1");
        assert_eq!(out[0].size, 2);
    }

    #[test]
    fn module_filter_matches_bare_path_or_qualified() {
        let dir = TempDir::new().unwrap();
        let graph = graph_with_sccs(vec![
            scc("scc:0", &["static/app::runtime/plugins"], false),
            scc("scc:1", &["static/app::runtime/other"], false),
        ]);
        let graph_path = write_graph(&dir, &graph);
        let mut args = base_args(graph_path.clone());
        args.module = Some("runtime/plugins".to_string());
        let out = collect(&args).unwrap();
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].id, "scc:0");

        let mut args = base_args(graph_path);
        args.module = Some("static/app::runtime/other".to_string());
        let out = collect(&args).unwrap();
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].id, "scc:1");
    }

    #[test]
    fn residual_only_keeps_residual_sccs() {
        let dir = TempDir::new().unwrap();
        let graph = graph_with_sccs(vec![
            scc("scc:0", &["static/app::residual/unhandled"], false),
            scc("scc:1", &["static/app::runtime/plugins"], false),
        ]);
        let graph_path = write_graph(&dir, &graph);
        let mut args = base_args(graph_path);
        args.residual_only = true;
        let out = collect(&args).unwrap();
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].id, "scc:0");
        assert!(out[0].all_residual);
    }

    #[test]
    fn cycles_only_and_singletons_only_conflict() {
        let dir = TempDir::new().unwrap();
        let graph = graph_with_sccs(Vec::new());
        let graph_path = write_graph(&dir, &graph);
        let mut args = base_args(graph_path);
        args.cycles_only = true;
        args.singletons_only = true;
        assert!(collect(&args).is_err());
    }

    #[test]
    fn binding_without_modules_root_errors() {
        let dir = TempDir::new().unwrap();
        let graph = graph_with_sccs(Vec::new());
        let graph_path = write_graph(&dir, &graph);
        let mut args = base_args(graph_path);
        args.binding = Some("XOe".to_string());
        assert!(collect(&args).is_err());
    }

    #[test]
    fn binding_resolves_to_module_and_filters_scc() {
        let dir = TempDir::new().unwrap();
        let modules_root = dir.path().join("modules");
        let runtime = modules_root.join("runtime");
        fs::create_dir_all(&runtime).unwrap();
        fs::write(
            runtime.join("plugins.yaml"),
            "members:\n  - selector:\n      binding:\n        name: XOe\n",
        )
        .unwrap();
        let graph = graph_with_sccs(vec![
            scc("scc:0", &["static/app::runtime/plugins"], false),
            scc("scc:1", &["static/app::runtime/other"], false),
        ]);
        let graph_path = write_graph(&dir, &graph);
        let mut args = base_args(graph_path);
        args.modules_root = Some(modules_root);
        args.binding = Some("XOe".to_string());
        let out = collect(&args).unwrap();
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].id, "scc:0");
        // silence unused-import warnings under cfg(test)
        let _ = ModuleReportRef {
            id: "x".to_string(),
            label: "x".to_string(),
            residual: false,
            index: None,
            target_file: None,
        };
    }
}
