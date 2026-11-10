//! `summary_effects`: measure the summary-based effect prototype on one chunk.
//!
//! Report-only. It reads a chunk and, optionally, the current analysis's
//! effect translation-validation report and conflict export for that chunk,
//! and writes one JSON report. It decides nothing.

use std::path::PathBuf;

use anyhow::{Context, Result};
use clap::Parser;
use summary_effects::generate::FunctionHeap;
use summary_effects::report::{Inputs, report, tier0_report};
use summary_effects::tier0;

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
    /// Which analysis to measure.
    #[arg(long, value_enum, default_value = "tier0")]
    mode: Mode,
    /// The pipeline's `modules.json` for the assignment, for module damage.
    #[arg(long)]
    modules: Option<PathBuf>,
    /// Include source text, identifiers and property names in the report.
    /// Without it the report holds only counts, ordinals, classes and
    /// timings.
    #[arg(long)]
    examples: bool,
    /// Tier 0: treat exported bindings as reachable by unknown code from the
    /// start (another chunk in an import cycle with this one).
    #[arg(long)]
    exports_reachable: bool,
    /// The heap abstraction for literals allocated inside function bodies.
    #[arg(long, value_enum, default_value = "function")]
    function_heap: FunctionHeap,
    /// Write the unproven swapped pairs here, one
    /// `earlier<TAB>later<TAB>class` line each, in source ordinals.
    #[arg(long)]
    unproven_pairs: Option<PathBuf>,
    #[arg(long)]
    out: Option<PathBuf>,
}

#[derive(Clone, Copy, PartialEq, Eq, clap::ValueEnum)]
enum Mode {
    /// Ordered impure statements plus reference reach, from syntax alone.
    Tier0,
    /// Inclusion-based points-to with per-function effect summaries.
    PointsTo,
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
    let modules = match &cli.modules {
        Some(path) => {
            let text = std::fs::read_to_string(path)
                .with_context(|| format!("reading {}", path.display()))?;
            Some(serde_json::from_str(&text)?)
        }
        None => None,
    };
    let (mode, examples) = (cli.mode, cli.examples);
    let pairs_out = cli.unproven_pairs;
    let options = tier0::Options {
        exports_reachable: cli.exports_reachable,
    };
    // Minified chunks nest expressions deeper than the default stack.
    let rendered = std::thread::Builder::new()
        .stack_size(STACK_BYTES)
        .spawn(move || {
            js_ast::with_swc_globals(|| -> Result<String> {
                let inputs = Inputs {
                    chunk: &chunk,
                    source,
                    order,
                    current_conflicts,
                    function_heap,
                    modules,
                    examples,
                    pairs_out,
                };
                Ok(match mode {
                    Mode::Tier0 => serde_json::to_string_pretty(&tier0_report(inputs, &options)?)?,
                    Mode::PointsTo => serde_json::to_string_pretty(&report(inputs)?)?,
                })
            })
        })?
        .join()
        .map_err(|_| anyhow::anyhow!("the analysis thread panicked"))??;
    match &cli.out {
        Some(path) => {
            std::fs::write(path, rendered).with_context(|| format!("writing {}", path.display()))?
        }
        None => println!("{rendered}"),
    }
    Ok(())
}
