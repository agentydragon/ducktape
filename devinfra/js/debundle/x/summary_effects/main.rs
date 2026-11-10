//! `summary_effects`: measure the summary-based effect prototype on one chunk.
//!
//! Report-only. It reads a chunk and, optionally, the current analysis's
//! effect translation-validation report and conflict export for that chunk,
//! and writes one JSON report. It decides nothing.

use std::path::PathBuf;

use anyhow::{Context, Result};
use clap::Parser;
use summary_effects::generate::FunctionHeap;
use summary_effects::report::{Inputs, report};

#[derive(Parser)]
struct Cli {
    /// The chunk's JavaScript source.
    #[arg(long)]
    chunk: PathBuf,
    /// The chunk's `effect_translation_validation.json`; its `emitted_order`
    /// selects the swapped pairs.
    #[arg(long, conflicts_with = "order")]
    effect_report: Option<PathBuf>,
    /// The emitted order as comma-separated source-unit ordinals.
    #[arg(long, value_delimiter = ',')]
    order: Option<Vec<usize>>,
    /// The chunk's rows of `debundle run --effect-conflicts-jsonl`.
    #[arg(long)]
    current_conflicts: Option<PathBuf>,
    /// The heap abstraction for literals allocated inside function bodies.
    #[arg(long, value_enum, default_value = "function")]
    function_heap: FunctionHeap,
    #[arg(long)]
    out: Option<PathBuf>,
}

const STACK_BYTES: usize = 1 << 30;

fn main() -> Result<()> {
    let cli = Cli::parse();
    let source = std::fs::read_to_string(&cli.chunk)
        .with_context(|| format!("reading {}", cli.chunk.display()))?;
    let order = match (&cli.effect_report, cli.order) {
        (Some(path), _) => {
            let text = std::fs::read_to_string(path)
                .with_context(|| format!("reading {}", path.display()))?;
            let value: serde_json::Value = serde_json::from_str(&text)?;
            Some(serde_json::from_value(value["emitted_order"].clone())?)
        }
        (None, order) => order,
    };
    let current_conflicts = match &cli.current_conflicts {
        Some(path) => {
            let text = std::fs::read_to_string(path)
                .with_context(|| format!("reading {}", path.display()))?;
            Some(
                text.lines()
                    .filter(|line| !line.trim().is_empty())
                    .map(serde_json::from_str)
                    .collect::<Result<Vec<serde_json::Value>, _>>()?,
            )
        }
        None => None,
    };
    let chunk = cli.chunk.display().to_string();
    let function_heap = cli.function_heap;
    // Minified chunks nest expressions deeper than the default stack.
    let report = std::thread::Builder::new()
        .stack_size(STACK_BYTES)
        .spawn(move || {
            js_ast::with_swc_globals(|| {
                report(Inputs {
                    chunk: &chunk,
                    source,
                    order,
                    current_conflicts,
                    function_heap,
                })
            })
        })?
        .join()
        .map_err(|_| anyhow::anyhow!("the analysis thread panicked"))??;
    let rendered = serde_json::to_string_pretty(&report)?;
    match &cli.out {
        Some(path) => {
            std::fs::write(path, rendered).with_context(|| format!("writing {}", path.display()))?
        }
        None => println!("{rendered}"),
    }
    Ok(())
}
