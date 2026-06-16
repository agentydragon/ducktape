//! `debundle scc` / `debundle cluster`: module-quotient SCC listing
//! and per-binding neighbor queries over a generated owner graph.

use anyhow::{Context, Result};
use clap::Args as ClapArgs;
use peel::{CommonArgs as PeelCommonArgs, OutputFormat, print_report, resolve_binding_owners};

/// Args for `debundle scc`.
#[derive(Debug, ClapArgs)]
pub struct SccArgs {
    #[command(flatten)]
    pub common: PeelCommonArgs,

    /// Restrict output to the SCC containing this binding's home module.
    #[arg(long = "binding")]
    pub binding: Option<String>,

    /// Restrict to SCCs that are true cycles (≥2 modules in a loop).
    #[arg(long = "cycles-only")]
    pub cycles_only: bool,

    /// Restrict to SCCs containing the residual catch-all module.
    #[arg(long = "residual-only")]
    pub residual_only: bool,

    /// Restrict to singleton (size-1) SCCs.
    #[arg(long = "singletons-only")]
    pub singletons_only: bool,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

/// Args for `debundle cluster <sym>`.
#[derive(Debug, ClapArgs)]
pub struct ClusterArgs {
    /// Binding identifier (minified or readable). May also be supplied
    /// as `--binding <sym>` (the spelling some operator skills use).
    pub sym: Option<String>,

    /// Alias for the positional `<sym>`.
    #[arg(long = "binding")]
    pub binding: Option<String>,

    #[command(flatten)]
    pub common: PeelCommonArgs,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

impl ClusterArgs {
    /// Resolve the binding from either the positional `<sym>` or the
    /// `--binding` alias; exactly one must be present.
    fn resolve_sym(&self) -> Result<&str> {
        match (self.sym.as_deref(), self.binding.as_deref()) {
            (Some(sym), None) | (None, Some(sym)) => Ok(sym),
            (Some(_), Some(_)) => {
                anyhow::bail!("pass the binding once: positional <sym> or --binding, not both")
            }
            (None, None) => {
                anyhow::bail!("missing binding: pass it positionally or as --binding <sym>")
            }
        }
    }
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct SccReport {
    pub sccs: Vec<SccEntry>,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct SccEntry {
    pub id: String,
    pub modules: Vec<String>,
    pub labels: Vec<String>,
    pub is_cycle: bool,
    pub realizable: bool,
}

/// A module-quotient node as both its interned `logical:N` id and its
/// human-readable path label, so cluster output is legible without a
/// second `describe` round-trip (CLI_DOGFOOD #2).
#[derive(Debug, Clone, serde::Serialize)]
pub struct ModuleRef {
    pub id: String,
    pub label: String,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct ClusterReport {
    pub binding: String,
    pub home_module: ModuleRef,
    pub incoming_modules: Vec<ModuleRef>,
    pub outgoing_modules: Vec<ModuleRef>,
}

pub fn run_scc(args: SccArgs) -> Result<()> {
    let graph = crate::load_owner_graph_report(&args.common.owner_graph_path)?;
    // If a binding was supplied, find its owner -> destination module
    // first; we restrict SCCs to ones containing that destination.
    // Uses the shared `resolve_binding_owners` helper so minified and
    // readable name forms both resolve.
    let restrict_to_module: Option<analysis::ModuleKey> = if let Some(sym) = &args.binding {
        let owner = resolve_binding_owners(&graph, sym)
            .into_iter()
            .next()
            .ok_or_else(|| anyhow::anyhow!("no owner declares binding {sym:?}"))?;
        Some(owner.destination.clone())
    } else {
        None
    };

    let mut entries: Vec<SccEntry> = Vec::new();
    for scc in &graph.quotient.sccs {
        let is_cycle = scc.is_cycle;
        let is_singleton = scc.modules.len() == 1;
        let touches_residual = scc.modules.iter().any(|m| graph.is_residual(m));
        if args.cycles_only && !is_cycle {
            continue;
        }
        if args.singletons_only && !is_singleton {
            continue;
        }
        if args.residual_only && !touches_residual {
            continue;
        }
        if let Some(want) = &restrict_to_module {
            if !scc.modules.contains(want) {
                continue;
            }
        }
        // Resolve each interned key to its human path via the module
        // table for the `labels` view; the wire stores the path once.
        let labels: Vec<String> = scc
            .modules
            .iter()
            .map(|key| {
                graph
                    .module(key)
                    .map(|entry| entry.path.to_string())
                    .unwrap_or_else(|| key.as_str().to_string())
            })
            .collect();
        entries.push(SccEntry {
            id: scc.id.clone(),
            modules: scc.modules.iter().map(|k| k.as_str().to_string()).collect(),
            labels,
            is_cycle,
            realizable: scc.realizable,
        });
    }
    let report = SccReport { sccs: entries };
    let format = OutputFormat::resolve(args.format);
    if format == OutputFormat::Ndjson {
        for entry in &report.sccs {
            println!("{}", serde_json::to_string(entry)?);
        }
        return Ok(());
    }
    print_report(&report, format, render_scc_text).context("writing scc output")
}

fn render_scc_text(report: &SccReport, out: &mut String) {
    out.push_str(&format!("{} scc(s)\n", report.sccs.len()));
    for scc in &report.sccs {
        let flags = match (scc.is_cycle, scc.realizable) {
            (true, true) => "[cycle,realizable]",
            (true, false) => "[cycle,UNREALIZABLE]",
            (false, _) => "",
        };
        out.push_str(&format!(
            "  {}  modules=[{}]  {}\n",
            scc.id,
            scc.modules.join(", "),
            flags
        ));
    }
}

pub fn run_cluster(args: ClusterArgs) -> Result<()> {
    let sym = args.resolve_sym()?;
    let graph = crate::load_owner_graph_report(&args.common.owner_graph_path)?;
    // Use the shared `resolve_binding_owners` helper (same code path
    // `describe` / `show-source` / `scc --binding` use), so minified
    // and readable names both work.
    let owner = resolve_binding_owners(&graph, sym)
        .into_iter()
        .next()
        .ok_or_else(|| anyhow::anyhow!("no owner declares binding {sym:?}"))?;
    let home_key = &owner.destination;
    // Dedup + sort by interned id; carry the path label alongside.
    let mut incoming: std::collections::BTreeMap<String, ModuleRef> =
        std::collections::BTreeMap::new();
    let mut outgoing: std::collections::BTreeMap<String, ModuleRef> =
        std::collections::BTreeMap::new();
    for edge in &graph.quotient.edges {
        if &edge.target == home_key && &edge.source != home_key {
            let module_ref = resolve_module_ref(&graph, &edge.source);
            incoming.insert(module_ref.id.clone(), module_ref);
        }
        if &edge.source == home_key && &edge.target != home_key {
            let module_ref = resolve_module_ref(&graph, &edge.target);
            outgoing.insert(module_ref.id.clone(), module_ref);
        }
    }
    let report = ClusterReport {
        binding: sym.to_string(),
        home_module: resolve_module_ref(&graph, home_key),
        incoming_modules: incoming.into_values().collect(),
        outgoing_modules: outgoing.into_values().collect(),
    };
    let format = OutputFormat::resolve(args.format);
    print_report(&report, format, render_cluster_text).context("writing cluster output")
}

/// Pair an interned module key with its human path label, falling back
/// to the raw key string when the module table has no entry for it.
fn resolve_module_ref(graph: &analysis::OwnerGraphReport, key: &analysis::ModuleKey) -> ModuleRef {
    ModuleRef {
        id: key.as_str().to_string(),
        label: graph
            .module(key)
            .map(|entry| entry.path.to_string())
            .unwrap_or_else(|| key.as_str().to_string()),
    }
}

fn render_cluster_text(report: &ClusterReport, out: &mut String) {
    out.push_str(&format!(
        "binding={} home={} ({})\n",
        report.binding, report.home_module.label, report.home_module.id,
    ));
    out.push_str(&format!(
        "  incoming: {}\n",
        join_module_labels(&report.incoming_modules)
    ));
    out.push_str(&format!(
        "  outgoing: {}\n",
        join_module_labels(&report.outgoing_modules)
    ));
}

fn join_module_labels(refs: &[ModuleRef]) -> String {
    refs.iter()
        .map(|module_ref| module_ref.label.as_str())
        .collect::<Vec<_>>()
        .join(", ")
}
