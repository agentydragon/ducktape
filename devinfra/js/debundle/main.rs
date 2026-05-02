use std::process::ExitCode;

use pipeline::{
    self, ParsedTransformCli, parse_transform_cli_args, render_transform_summary, run_transform_cli,
};

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
    let argv = std::env::args().skip(1).collect::<Vec<_>>();
    match parse_transform_cli_args(&argv)? {
        ParsedTransformCli::Help => {
            print!("{}", pipeline::transform_cli_help());
            Ok(ExitCode::SUCCESS)
        }
        ParsedTransformCli::Run(cli) => {
            let summary = run_transform_cli(&cli)?;
            print!("{}", render_transform_summary(&summary));
            Ok(ExitCode::SUCCESS)
        }
    }
}
