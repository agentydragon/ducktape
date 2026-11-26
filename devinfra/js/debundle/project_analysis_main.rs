//! Small Bazel entry point for the canonical analysis projection.

use analysis_thread::run_analysis_command;
use clap::{CommandFactory, FromArgMatches, Parser};
use transform_command::ProjectAnalysisArgs;
use transform_inputs::validate_transform_arg_matches;

#[derive(Parser)]
struct Args {
    #[command(flatten)]
    project: ProjectAnalysisArgs,
}

fn main() -> std::process::ExitCode {
    run_analysis_command(|| {
        let matches = Args::command().get_matches();
        validate_transform_arg_matches(&matches)?;
        Args::from_arg_matches(&matches)?
            .project
            .run(&mut std::io::stderr())
    })
}
