//! Bazel's analysis-only executable; the standalone CLI retains `analyze`.

use analysis_thread::run_analysis;
use analyze_command::AnalyzeArgs;
use clap::Parser;

#[derive(Parser)]
struct Args {
    #[command(flatten)]
    analysis: AnalyzeArgs,
}

fn main() -> std::process::ExitCode {
    run_analysis(|| {
        match Args::parse()
            .analysis
            .run(analysis_identity::ANALYSIS_ENGINE_SHA256)
        {
            Ok(()) => std::process::ExitCode::SUCCESS,
            Err(error) => {
                eprintln!("{error:#}");
                std::process::ExitCode::FAILURE
            }
        }
    })
}
