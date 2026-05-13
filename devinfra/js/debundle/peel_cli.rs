//! Unified peel CLI frontend.
//!
//! `peel_cli` is the entry point for the three peel views — factorize
//! (module-partition proposals), horizon (deferred-YAML coverage ranking), and
//! inventory (per-binding peelability).

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
// Per-view runners.
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
