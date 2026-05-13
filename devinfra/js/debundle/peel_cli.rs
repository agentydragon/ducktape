//! Unified peel CLI frontend.
//!
//! `peel_cli` is the preferred entry point for the three peel views — factorize
//! (module-partition proposals), horizon (deferred-YAML coverage ranking), and
//! inventory (per-binding peelability). The three historical binaries
//! (`peel_factorize_cli`, `peel_horizon_cli`, `peel_inventory_cli`) are kept as
//! thin shims that dispatch into the per-view entry points exposed here, so
//! external callers (gaffer's tana-peel skill) keep working unchanged. New
//! callers should use the unified binary.

use std::ffi::OsString;
use std::path::PathBuf;
use std::process::ExitCode;

use anyhow::{Context, Result};
use clap::{Parser, ValueEnum};
use peel_factorize::{PeelFactorizeOptions, analyze_peel_factorize};
use peel_horizon::{PeelHorizonOptions, analyze_peel_horizon, render_peel_horizon_report};
use peel_inventory::{InventoryView, PeelInventoryOptions, build_inventory, render_inventory};

#[derive(Debug, Clone, Copy, ValueEnum, PartialEq, Eq)]
pub enum PeelView {
    /// Propose module partitions from owner_graph.json + spec modules tree
    /// (annotated FactorizeReport cells). Always JSON.
    Factorize,
    /// Rank `*.yaml.deferred` files by member coverage against the owner graph.
    /// Text by default; `--json` for the underlying report.
    Horizon,
    /// Per-binding view of individually peelable bindings, with proposed_dir
    /// heuristic and forbidden blockers. Text by default; `--json` for JSON.
    Inventory,
}

#[derive(Debug, Parser)]
#[command(
    name = "peel",
    about = "Unified peel-planning CLI. Dispatches to factorize / horizon / inventory views over a debundle owner_graph.json + spec modules tree."
)]
pub struct UnifiedArgs {
    /// Which peel view to render.
    #[arg(long, value_enum)]
    pub view: PeelView,

    /// Path to `owner_graph.json` (debundler analysis output).
    #[arg(long = "graph")]
    pub owner_graph_path: PathBuf,

    /// Root of `*.yaml` / `*.yaml.deferred` spec files.
    #[arg(long = "modules")]
    pub modules_root: PathBuf,

    /// Hard line ceiling per emitted cell (factorize view).
    #[arg(long = "size-cap-lines", default_value_t = 2000)]
    pub size_cap_lines: usize,

    /// Row limit (horizon and inventory views).
    #[arg(long, default_value_t = 0)]
    pub limit: usize,

    /// Near-missing companion threshold (horizon view).
    #[arg(long = "near-missing", default_value_t = 2)]
    pub near_missing: usize,

    /// Max companion bindings to surface per near-miss row (horizon view).
    #[arg(long = "max-companions", default_value_t = 16)]
    pub max_companions: usize,

    /// Filter inventory to candidates with at least one renamed export.
    #[arg(long = "readable-only")]
    pub readable_only: bool,

    /// Group inventory rows by `proposed_dir` (inventory view).
    #[arg(long = "by-destination")]
    pub by_destination: bool,

    /// Emit JSON instead of human-readable output.
    /// Factorize view is always JSON regardless of this flag.
    #[arg(long)]
    pub json: bool,
}

/// Run the unified CLI from argv. Returns the appropriate exit code.
pub fn run_unified() -> ExitCode {
    real_unified(UnifiedArgs::parse())
}

fn real_unified(args: UnifiedArgs) -> ExitCode {
    let result = match args.view {
        PeelView::Factorize => run_factorize(
            args.owner_graph_path,
            args.modules_root,
            args.size_cap_lines,
        ),
        PeelView::Horizon => run_horizon(
            args.owner_graph_path,
            args.modules_root,
            if args.limit == 0 { 40 } else { args.limit },
            args.near_missing,
            args.max_companions,
            args.json,
        ),
        PeelView::Inventory => run_inventory(
            args.owner_graph_path,
            args.modules_root,
            if args.limit == 0 { 200 } else { args.limit },
            args.readable_only,
            args.by_destination,
            args.json,
        ),
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("{error:#}");
            ExitCode::from(1)
        }
    }
}

// ---------------------------------------------------------------------------
// Per-view entry points used by the back-compat shim binaries.
// ---------------------------------------------------------------------------

#[derive(Debug, Parser)]
#[command(
    name = "peel_factorize",
    about = "Propose module partitions from a debundle owner_graph.json + spec modules tree. Emits JSON proposals on stdout. (Back-compat shim — prefer `peel_cli --view factorize`.)"
)]
struct FactorizeArgs {
    #[arg(long = "graph")]
    owner_graph_path: PathBuf,
    #[arg(long = "modules")]
    modules_root: PathBuf,
    /// Hard line ceiling per emitted cell. Cells exceeding the cap
    /// are still emitted (with `oversize: true`); the algorithm
    /// never manufactures structural splits to fit.
    #[arg(long = "size-cap-lines", default_value_t = 2000)]
    size_cap_lines: usize,
}

pub fn factorize_main() -> ExitCode {
    run_shim(|argv| {
        let args = FactorizeArgs::try_parse_from(argv)?;
        run_factorize(
            args.owner_graph_path,
            args.modules_root,
            args.size_cap_lines,
        )
    })
}

#[derive(Debug, Parser)]
#[command(
    name = "peel_horizon",
    about = "Rank tree-shaped module YAMLs against a debundle owner_graph.json peelability report. (Back-compat shim — prefer `peel_cli --view horizon`.)"
)]
struct HorizonArgs {
    #[arg(long = "graph")]
    owner_graph_path: PathBuf,
    #[arg(long = "modules")]
    modules_root: PathBuf,
    #[arg(long, default_value_t = 40)]
    limit: usize,
    #[arg(long, default_value_t = 2)]
    near_missing: usize,
    #[arg(long, default_value_t = 16)]
    max_companions: usize,
    #[arg(long)]
    json: bool,
}

pub fn horizon_main() -> ExitCode {
    run_shim(|argv| {
        let args = HorizonArgs::try_parse_from(argv)?;
        run_horizon(
            args.owner_graph_path,
            args.modules_root,
            args.limit,
            args.near_missing,
            args.max_companions,
            args.json,
        )
    })
}

#[derive(Debug, Parser)]
#[command(
    name = "peel_inventory",
    about = "Emit a parseable inventory of peelable bindings from a debundle owner_graph.json. (Back-compat shim — prefer `peel_cli --view inventory`.)"
)]
struct InventoryArgs {
    #[arg(long = "graph")]
    owner_graph_path: PathBuf,
    #[arg(long = "modules")]
    modules_root: PathBuf,
    #[arg(long = "readable-only")]
    readable_only: bool,
    #[arg(long, default_value_t = 200)]
    limit: usize,
    #[arg(long = "by-destination")]
    by_destination: bool,
    #[arg(long)]
    json: bool,
}

pub fn inventory_main() -> ExitCode {
    run_shim(|argv| {
        let args = InventoryArgs::try_parse_from(argv)?;
        run_inventory(
            args.owner_graph_path,
            args.modules_root,
            args.limit,
            args.readable_only,
            args.by_destination,
            args.json,
        )
    })
}

fn run_shim<F>(body: F) -> ExitCode
where
    F: FnOnce(Vec<OsString>) -> Result<()>,
{
    let argv: Vec<OsString> = std::env::args_os().collect();
    match body(argv) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            // clap parse errors should print themselves and exit cleanly via
            // their built-in handling. We funnel them through here so the
            // exit code matches the unified CLI's behavior.
            if let Some(clap_err) = error.downcast_ref::<clap::Error>() {
                clap_err.exit();
            }
            eprintln!("{error:#}");
            ExitCode::from(1)
        }
    }
}

// ---------------------------------------------------------------------------
// Shared per-view runners.
// ---------------------------------------------------------------------------

fn run_factorize(
    owner_graph_path: PathBuf,
    modules_root: PathBuf,
    size_cap_lines: usize,
) -> Result<()> {
    let report = analyze_peel_factorize(&PeelFactorizeOptions {
        owner_graph_path,
        modules_root,
        size_cap_lines,
    })?;
    println!(
        "{}",
        serde_json::to_string_pretty(&report).context("serialize PeelFactorizeReport")?
    );
    Ok(())
}

fn run_horizon(
    owner_graph_path: PathBuf,
    modules_root: PathBuf,
    limit: usize,
    near_missing: usize,
    max_companions: usize,
    json: bool,
) -> Result<()> {
    let report = analyze_peel_horizon(&PeelHorizonOptions {
        owner_graph_path,
        modules_root,
        near_missing,
        max_companions,
    })?;
    if json {
        println!(
            "{}",
            serde_json::to_string_pretty(&report).context("serialize PeelHorizonReport")?
        );
    } else {
        print!(
            "{}",
            render_peel_horizon_report(&report, limit, max_companions, near_missing)
        );
    }
    Ok(())
}

fn run_inventory(
    owner_graph_path: PathBuf,
    modules_root: PathBuf,
    limit: usize,
    readable_only: bool,
    by_destination: bool,
    json: bool,
) -> Result<()> {
    let mut inventory = build_inventory(&PeelInventoryOptions {
        owner_graph_path,
        modules_root,
    })?;
    if readable_only {
        inventory.retain(|record| record.has_readable);
    }
    let view = if json {
        InventoryView::Json
    } else if by_destination {
        InventoryView::ByDestination { limit }
    } else {
        InventoryView::Flat { limit }
    };
    print!("{}", render_inventory(&inventory, view));
    if json {
        println!();
    }
    Ok(())
}
