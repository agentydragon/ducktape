//! Small Bazel entry point for the canonical analysis projection.

use analysis_thread::run_analysis;
use clap::{CommandFactory, FromArgMatches, Parser};
use transform_command::{ProjectAnalysisArgs, validate_transform_arg_matches};

#[derive(Parser)]
struct Args {
    #[command(flatten)]
    project: ProjectAnalysisArgs,
}

fn main() -> std::process::ExitCode {
    run_analysis(|| {
        let matches = Args::command().get_matches();
        let result = (|| -> anyhow::Result<()> {
            validate_transform_arg_matches(&matches)?;
            Args::from_arg_matches(&matches)?.project.run()
        })();
        match result {
            Ok(()) => std::process::ExitCode::SUCCESS,
            Err(error) => {
                eprintln!("{error:#}");
                std::process::ExitCode::FAILURE
            }
        }
    })
}
