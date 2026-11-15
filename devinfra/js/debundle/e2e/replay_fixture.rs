//! Replay a retained public test case with an explicitly supplied executable.

use clap::{Parser, Subcommand};
use debundle_e2e_support::{recorded_commands, replay_command};
use std::io::{self, Write};
use std::os::unix::process::ExitStatusExt;
use std::path::PathBuf;

#[derive(Parser)]
struct Args {
    #[command(subcommand)]
    command: Replay,
}

#[derive(Subcommand)]
enum Replay {
    /// List recorded commands and their original executable paths.
    List { case: PathBuf },
    /// Restore this command's inputs in a fresh directory and execute it.
    Run {
        case: PathBuf,
        command: String,
        #[arg(long)]
        out: PathBuf,
        #[arg(long)]
        binary: PathBuf,
        #[arg(long)]
        allow_tool_change: bool,
    },
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    match Args::parse().command {
        Replay::List { case } => {
            for command in recorded_commands(&case)? {
                println!("{} {:?} {:?}", command.id, command.program, command.args);
            }
        }
        Replay::Run {
            case,
            command,
            out,
            binary,
            allow_tool_change,
        } => {
            let output = replay_command(&case, &command, &out, &binary, allow_tool_change)?;
            io::stdout().write_all(&output.stdout)?;
            io::stderr().write_all(&output.stderr)?;
            std::process::exit(
                output
                    .status
                    .code()
                    .unwrap_or_else(|| 128 + output.status.signal().unwrap_or(1)),
            );
        }
    }
    Ok(())
}
