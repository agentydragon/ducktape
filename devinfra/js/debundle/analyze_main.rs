//! Bazel's analysis-only executable; the standalone CLI retains `analyze`.

use clap::Parser;
use staged_analysis::AnalyzeArgs;
use swc_common::{GLOBALS, Globals};

#[derive(Parser)]
struct Args {
    #[command(flatten)]
    analysis: AnalyzeArgs,
}

fn main() -> std::process::ExitCode {
    GLOBALS.set(&Globals::default(), || {
        match staged_analysis::analyze_cli(
            Args::parse().analysis,
            analysis_identity::ANALYSIS_ENGINE_SHA256,
        ) {
            Ok(()) => std::process::ExitCode::SUCCESS,
            Err(error) => {
                eprintln!("{error:#}");
                std::process::ExitCode::FAILURE
            }
        }
    })
}
