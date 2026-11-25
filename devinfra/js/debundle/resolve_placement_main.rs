//! Small Bazel entry point for the authored placement resolution.

use analysis_thread::run_analysis;
use clap::{CommandFactory, FromArgMatches, Parser};
use resolved_placement::ResolvePlacementArgs;
use transform_inputs::validate_transform_arg_matches;

#[derive(Parser)]
struct Args {
    #[command(flatten)]
    resolve: ResolvePlacementArgs,
}

fn main() -> std::process::ExitCode {
    run_analysis(|| {
        let matches = Args::command().get_matches();
        let result = (|| -> anyhow::Result<()> {
            validate_transform_arg_matches(&matches)?;
            Args::from_arg_matches(&matches)?
                .resolve
                .run(&mut std::io::stderr())
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
