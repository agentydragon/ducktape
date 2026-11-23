use clap::Parser;
use std::str::FromStr;

#[derive(Clone, Debug, PartialEq)]
pub enum Command {
    Snapshot,
    Csv,
    ModelLastSnapshot,
    // TODO: implement
    Server,
}

impl FromStr for Command {
    type Err = &'static str;
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "snapshot" => Ok(Command::Snapshot),
            "csv" => Ok(Command::Csv),
            "modellastsnapshot" => Ok(Command::ModelLastSnapshot),
            "server" => Ok(Command::Server),
            _ => Err("unknown command"),
        }
    }
}

#[derive(Debug, Parser, PartialEq)]
pub struct Opt {
    #[arg(
        long,
        help = "command; one of snapshot, csv, modellastsnapshot, server",
        default_value = "snapshot"
    )]
    pub command: Command,
}
