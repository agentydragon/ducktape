//! Output formatting helpers shared by query subcommands.
//!
//! The default output format is pretty JSON. With `--ndjson`, each
//! record from a `Vec<T>` is printed on its own line so the stream
//! is `jq -c` / `grep` friendly without further reshaping.

use anyhow::Result;
use serde::Serialize;

#[derive(Debug, Clone, Copy, Default, clap::Args)]
pub struct OutputFormat {
    /// Emit one JSON record per line instead of one pretty-printed
    /// document. Listing commands use this to stream records to
    /// `jq -c` / `grep` without wrapping them in an array.
    #[arg(long = "ndjson", default_value_t = false)]
    pub ndjson: bool,
}

pub fn print_json<T: Serialize>(value: &T) -> Result<()> {
    println!("{}", serde_json::to_string_pretty(value)?);
    Ok(())
}

pub fn print_records<T: Serialize>(records: &[T], format: OutputFormat) -> Result<()> {
    if format.ndjson {
        for record in records {
            println!("{}", serde_json::to_string(record)?);
        }
    } else {
        print_json(&records)?;
    }
    Ok(())
}
