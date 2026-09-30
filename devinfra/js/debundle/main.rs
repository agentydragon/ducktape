use std::process::ExitCode;

use clap::Parser;
use debundle_cli::{DebundleArgs, run_debundle_cli};
use swc_common::{GLOBALS, Globals};

fn main() -> ExitCode {
    // SWC hygiene (`Mark`, `SyntaxContext`) is stored in a thread-local
    // arena managed by `GLOBALS`. Every parse and AST-touching operation
    // in the debundler runs inside this closure so the `resolver` pass
    // can mint marks and downstream consumers can compare `Id`s. A single
    // `Globals` instance per process keeps `Mark` identity comparable
    // across chunks.
    let globals = Globals::default();
    GLOBALS.set(&globals, || match run_debundle_cli(DebundleArgs::parse()) {
        Ok(()) => ExitCode::SUCCESS,
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
