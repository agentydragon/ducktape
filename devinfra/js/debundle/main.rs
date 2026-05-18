use std::process::ExitCode;

use clap::Parser;
use pipeline::{TransformArgs, render_transform_summary, run_transform_cli};
use swc_common::{GLOBALS, Globals};

fn main() -> ExitCode {
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
            eprintln!("{error}");
            ExitCode::from(1)
        }
    })
}

fn real_main() -> anyhow::Result<ExitCode> {
    let cli = TransformArgs::parse().resolve()?;
    let summary = run_transform_cli(&cli)?;
    print!("{}", render_transform_summary(&summary));
    Ok(ExitCode::SUCCESS)
}
