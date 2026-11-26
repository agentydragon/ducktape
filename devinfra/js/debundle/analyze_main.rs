//! Bazel's analysis-only executable; the standalone CLI retains `analyze`.

use analysis_thread::run_analysis_command;
use analyze_command::AnalyzeArgs;
use clap::Parser;

#[derive(Parser)]
struct Args {
    #[command(flatten)]
    analysis: AnalyzeArgs,
}

fn main() -> std::process::ExitCode {
    run_analysis_command(|| {
        Args::parse().analysis.run(
            &mut std::io::stderr(),
            analysis_identity::ANALYSIS_ENGINE_SHA256,
        )
    })
}
