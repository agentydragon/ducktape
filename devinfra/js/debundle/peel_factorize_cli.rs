use std::process::ExitCode;

use clap::Parser;
use peel_factorize::{PeelFactorizeOptions, analyze_peel_factorize};

#[derive(Debug, Parser)]
#[command(
    name = "peel_factorize",
    about = "Propose module partitions from a debundle owner_graph.json + spec modules tree. Emits JSON proposals on stdout."
)]
struct Args {
    #[arg(long = "graph")]
    owner_graph_path: std::path::PathBuf,
    #[arg(long = "modules")]
    modules_root: std::path::PathBuf,
    /// Hard line ceiling per emitted cell. Cells exceeding the cap
    /// are still emitted (with `oversize: true`); the algorithm
    /// never manufactures structural splits to fit.
    #[arg(long = "size-cap-lines", default_value_t = 2000)]
    size_cap_lines: usize,
}

fn main() -> ExitCode {
    match real_main() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("{error:#}");
            ExitCode::from(1)
        }
    }
}

fn real_main() -> anyhow::Result<()> {
    let args = Args::parse();
    let report = analyze_peel_factorize(&PeelFactorizeOptions {
        owner_graph_path: args.owner_graph_path,
        modules_root: args.modules_root,
        size_cap_lines: args.size_cap_lines,
    })?;
    println!("{}", serde_json::to_string_pretty(&report)?);
    Ok(())
}
