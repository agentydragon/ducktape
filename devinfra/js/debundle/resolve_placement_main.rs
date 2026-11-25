//! Small Bazel entry point for the authored placement resolution.

use analysis_thread::run_analysis;
use clap::{Args, Command, FromArgMatches};
use resolved_placement::{ResolvePlacementArgs, ResolvePlacementsArgs};
use transform_inputs::validate_transform_arg_matches;

fn main() -> std::process::ExitCode {
    run_analysis(|| {
        let matches =
            ResolvePlacementArgs::augment_args(Command::new("debundle_resolve_placement"))
                .subcommand(ResolvePlacementsArgs::augment_args(
                    Command::new("batch").about("Resolve several chunks in one selector solve"),
                ))
                .subcommand_negates_reqs(true)
                .args_conflicts_with_subcommands(true)
                .get_matches();
        let result = (|| -> anyhow::Result<()> {
            validate_transform_arg_matches(&matches)?;
            let stderr = &mut std::io::stderr();
            match matches.subcommand_matches("batch") {
                Some(args) => ResolvePlacementsArgs::from_arg_matches(args)?.run(stderr),
                None => ResolvePlacementArgs::from_arg_matches(&matches)?.run(stderr),
            }
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
