//! `summary_effects`: measure the summary-based effect prototype on one chunk.
//!
//! Report-only. It reads a chunk and, optionally, the current analysis's
//! effect translation-validation report and conflict export for that chunk,
//! and writes one JSON report. It decides nothing.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use clap::Parser;
use summary_effects::generate::FunctionHeap;
use summary_effects::report::{Inputs, report, tier0_report_indexed};
use summary_effects::tier0;

#[derive(Parser)]
struct Cli {
    /// The chunk's JavaScript source.
    #[arg(long, required_unless_present = "manifest")]
    chunk: Option<PathBuf>,
    /// Tier 0: a JSON array of chunks to analyse against one read of the
    /// program. Each entry has `chunk` and optionally `effect_report`,
    /// `order`, `current_conflicts`, `modules`, `premises`,
    /// `unproven_pairs` and `out`, as the flags of those names.
    #[arg(long, conflicts_with_all = ["chunk", "effect_report", "order", "current_conflicts", "modules", "premises", "unproven_pairs", "out"])]
    manifest: Option<PathBuf>,
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
    /// Tier 0: a directory holding every chunk of the program, this one
    /// included, to decide whether the chunk is on an import cycle and what
    /// any chunk writes on globals.
    #[arg(long)]
    program_chunks: Option<PathBuf>,
    /// The pipeline's `js-files.txt` for `--program-chunks`: its
    /// `alias<TAB>original` lines map imported file names to stored ones.
    #[arg(long, requires = "program_chunks")]
    program_file_list: Option<PathBuf>,
    /// A prefix of absolute import specifiers that stands for the
    /// `--program-chunks` directory. Repeatable.
    #[arg(long, requires = "program_chunks")]
    program_specifier_root: Vec<String>,
    /// Tier 0: derive callee contracts from their bodies, across chunks with
    /// `--program-chunks`.
    #[arg(long)]
    derive: bool,
    /// Tier 0: bounded widenings of "pure definition" to enable.
    #[arg(long, value_enum, value_delimiter = ',')]
    upgrade: Vec<tier0::Upgrade>,
    /// Tier 0, estimate only (unsound): count statements impure for these
    /// causes as pure definitions.
    #[arg(long, value_enum, value_delimiter = ',')]
    what_if_pure: Vec<tier0::ImpureCause>,
    /// Tier 0: trusted callee premises, as JSON (`premises.rs`).
    #[arg(long)]
    premises: Option<PathBuf>,
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
#[value(rename_all = "snake_case")]
enum Mode {
    /// Ordered impure statements plus reference reach, from syntax alone.
    Tier0,
    /// Inclusion-based points-to with per-function effect summaries.
    PointsTo,
}

/// One chunk to analyse and where its inputs and report are.
#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Job {
    chunk: PathBuf,
    effect_report: Option<PathBuf>,
    order: Option<Vec<usize>>,
    current_conflicts: Option<PathBuf>,
    modules: Option<PathBuf>,
    premises: Option<PathBuf>,
    unproven_pairs: Option<PathBuf>,
    out: Option<PathBuf>,
}

fn read(path: &Path) -> Result<String> {
    std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))
}

fn parsed<T: serde::de::DeserializeOwned>(path: &Path) -> Result<T> {
    serde_json::from_str(&read(path)?).with_context(|| format!("parsing {}", path.display()))
}

/// The inputs of a job, read.
fn inputs<'a>(job: &'a Job, chunk: &'a str, cli: &Cli) -> Result<Inputs<'a>> {
    let order = match (&job.effect_report, &job.order) {
        (Some(path), _) => {
            let value: serde_json::Value = parsed(path)?;
            Some(serde_json::from_value(value["emitted_order"].clone())?)
        }
        (None, order) => order.clone(),
    };
    let current_conflicts = match &job.current_conflicts {
        Some(path) => Some(
            read(path)?
                .lines()
                .filter(|line| !line.trim().is_empty())
                .map(serde_json::from_str)
                .collect::<Result<Vec<serde_json::Value>, _>>()?,
        ),
        None => None,
    };
    Ok(Inputs {
        chunk,
        source: read(&job.chunk)?,
        order,
        current_conflicts,
        function_heap: cli.function_heap,
        modules: job.modules.as_deref().map(parsed).transpose()?,
        examples: cli.examples,
        pairs_out: job.unproven_pairs.clone(),
    })
}

fn write(out: Option<&Path>, rendered: String) -> Result<()> {
    match out {
        Some(path) => {
            std::fs::write(path, rendered).with_context(|| format!("writing {}", path.display()))
        }
        None => {
            println!("{rendered}");
            Ok(())
        }
    }
}

/// Analyses every job against one read of the program.
fn run(cli: &Cli, jobs: &[Job]) -> Result<()> {
    let Some(first) = jobs.first() else {
        return Ok(());
    };
    if cli.mode == Mode::PointsTo {
        anyhow::ensure!(cli.manifest.is_none(), "a manifest is for tier 0");
        let chunk = first.chunk.display().to_string();
        let rendered = serde_json::to_string_pretty(&report(inputs(first, &chunk, cli)?)?)?;
        return write(first.out.as_deref(), rendered);
    }
    let started = std::time::Instant::now();
    let program = cli
        .program_chunks
        .as_deref()
        .map(|root| {
            summary_effects::program::load(
                root,
                &first.chunk,
                cli.program_file_list.as_deref(),
                &cli.program_specifier_root,
            )
        })
        .transpose()?;
    let mut options = tier0::Options {
        exports_reachable: cli.exports_reachable,
        upgrades: cli.upgrade.iter().copied().collect(),
        what_if_pure: cli.what_if_pure.iter().copied().collect(),
        program,
        premises: Default::default(),
        derive: cli.derive,
    };
    // What derivation works out about the program's chunks serves every
    // job.
    let mut index =
        summary_effects::derive::ProgramIndex::new(options.program.as_ref().filter(|_| cli.derive));
    let (program_ms, started) = (started.elapsed().as_millis(), std::time::Instant::now());
    // One chunk that cannot be analysed does not stop the others.
    let mut failed = 0;
    for job in jobs {
        let chunk = job.chunk.display().to_string();
        let premises = match &job.premises {
            Some(path) => parsed(path),
            None => Ok(Default::default()),
        };
        let done = match premises {
            Ok(premises) => {
                options.premises = premises;
                let options = &options;
                let index = &mut index;
                // A panic leaves the index without what it had worked out,
                // which the next chunk works out again.
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| -> Result<()> {
                    if let Some(program) = &options.program {
                        program.retarget(&job.chunk)?;
                    }
                    let inputs = inputs(job, &chunk, cli)?;
                    let report = tier0_report_indexed(inputs, options, index)?;
                    write(job.out.as_deref(), serde_json::to_string_pretty(&report)?)
                }))
                .unwrap_or_else(|_| Err(anyhow::anyhow!("the analysis panicked")))
            }
            Err(error) => Err(error),
        };
        if let Err(error) = done {
            anyhow::ensure!(cli.manifest.is_some(), error);
            eprintln!("{chunk}: {error:#}");
            failed += 1;
        }
    }
    if cli.manifest.is_some() {
        eprintln!(
            "program read {program_ms} ms; {} chunks analysed in {} ms",
            jobs.len(),
            started.elapsed().as_millis()
        );
    }
    anyhow::ensure!(failed == 0, "{failed} of {} chunks failed", jobs.len());
    Ok(())
}

// Minified chunks nest expressions deeper than the default stack.
const STACK_BYTES: usize = 1 << 30;

fn main() -> Result<()> {
    let cli = Cli::parse();
    let jobs: Vec<Job> = match (&cli.manifest, &cli.chunk) {
        (Some(manifest), _) => parsed(manifest)?,
        (None, Some(chunk)) => vec![Job {
            chunk: chunk.clone(),
            effect_report: cli.effect_report.clone(),
            order: cli.order.clone(),
            current_conflicts: cli.current_conflicts.clone(),
            modules: cli.modules.clone(),
            premises: cli.premises.clone(),
            unproven_pairs: cli.unproven_pairs.clone(),
            out: cli.out.clone(),
        }],
        (None, None) => unreachable!("clap requires a chunk or a manifest"),
    };
    std::thread::Builder::new()
        .stack_size(STACK_BYTES)
        .spawn(move || js_ast::with_swc_globals(|| run(&cli, &jobs)))?
        .join()
        .map_err(|_| anyhow::anyhow!("the analysis thread panicked"))?
}
