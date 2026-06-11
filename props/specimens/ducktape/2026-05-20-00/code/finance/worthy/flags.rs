use std::path::PathBuf;
use std::str::FromStr;
use structopt::StructOpt;

#[derive(Debug, StructOpt, PartialEq)]
pub enum Command {
    // TODO: implement
    Snapshot,
    // TODO: implement
    Csv,
    // TODO: implement
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

#[derive(Debug, StructOpt, PartialEq)]
pub struct Opt {
    // TODO: implement
    #[structopt(
        long = "json_output_path",
        help = "path where JSON snapshot will be written"
    )]
    pub json_output_path: Option<PathBuf>,

    #[structopt(
        long,
        help = "command; one of snapshot, csv, modellastsnapshot, server",
        default_value = "snapshot"
    )]
    pub command: Command,
}
