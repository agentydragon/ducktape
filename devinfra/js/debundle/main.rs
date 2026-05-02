use std::process::ExitCode;

use clap::Parser;
use pipeline::{TransformArgs, render_transform_summary, run_transform_cli};

fn main() -> ExitCode {
    match real_main() {
        Ok(code) => code,
        Err(error) => {
            eprintln!("{error}");
            ExitCode::from(1)
        }
    }
}

fn real_main() -> anyhow::Result<ExitCode> {
    let cli = TransformArgs::parse().resolve();
    let summary = run_transform_cli(&cli)?;
    print!("{}", render_transform_summary(&summary));
    Ok(ExitCode::SUCCESS)
}
