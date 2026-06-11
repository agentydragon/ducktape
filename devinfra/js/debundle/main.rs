use std::process::ExitCode;

use clap::Parser;
use debundle_cli::{DebundleArgs, run_debundle_cli};
use gate::SccTimingReporter;
use swc_common::{GLOBALS, Globals};

fn main() -> ExitCode {
    // Install the realizability gate-path perf counter reporter when
    // DEBUNDLE_TIMING=1 is set. Returns `None` when the env var is
    // unset, so normal runs do not print a report. Cheap counters are
    // always recorded; the env var gates wall-clock timing, reporting,
    // and the expensive shadow base-Tarjan measurement. See
    // `devinfra/js/debundle/perf/proposer.md` (§"Gate perf counters")
    // for the counter list + example output.
    let _gate_perf_guard = SccTimingReporter::install_if_enabled();

    // SWC hygiene (`Mark`, `SyntaxContext`) is stored in a thread-local
    // arena managed by `GLOBALS`. Every parse and AST-touching operation
    // in the debundler runs inside this closure so the `resolver` pass
    // can mint marks and downstream consumers can compare `Id`s. A single
    // `Globals` instance per process keeps `Mark` identity comparable
    // across chunks.
    let globals = Globals::default();
    GLOBALS.set(&globals, || match real_main() {
        Ok(code) => code,
        Err(error) => {
            // `{error:#}` prints the full anyhow context chain inline
            // (top + every `with_context` cause separated by `: `).
            // Plain `{error}` only shows the topmost context, which
            // silently hides the actionable cause — e.g. parsing a
            // module YAML with an unknown field would report only
            // `parsing path/to/file.yaml` with no hint of the bad
            // field name.
            eprintln!("{error:#}");
            ExitCode::from(1)
        }
    })
}

fn real_main() -> anyhow::Result<ExitCode> {
    run_debundle_cli(DebundleArgs::parse())?;
    Ok(ExitCode::SUCCESS)
}
