//! Small Bazel entry point for the canonical analysis projection.

use clap::{CommandFactory, FromArgMatches, Parser};
use swc_common::{GLOBALS, Globals};
use transform_command::{ProjectAnalysisArgs, validate_transform_arg_matches};

#[derive(Parser)]
struct Args {
    #[command(flatten)]
    project: ProjectAnalysisArgs,
}

fn main() -> std::process::ExitCode {
    GLOBALS.set(&Globals::default(), || {
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
