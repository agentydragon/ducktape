//! Small Bazel entry point for the canonical analysis projection.

use clap::Parser;
use pipeline::ProjectAnalysisArgs;
use swc_common::{GLOBALS, Globals};

#[derive(Parser)]
struct Args {
    #[command(flatten)]
    project: ProjectAnalysisArgs,
}

fn main() -> std::process::ExitCode {
    GLOBALS.set(&Globals::default(), || match Args::parse().project.run() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("{error:#}");
            std::process::ExitCode::FAILURE
        }
    })
}
