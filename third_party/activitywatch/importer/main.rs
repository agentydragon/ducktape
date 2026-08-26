use std::path::PathBuf;
use std::process::ExitCode;

use aw_datastore::Datastore;
use aw_importer::import_snapshots;
use clap::Parser;

/// Idempotent one-way importer of per-device ActivityWatch snapshots into a
/// central aw-server-rust datastore.
#[derive(Parser)]
struct Args {
    /// Central aw-server-rust SQLite datastore to import into (created if absent).
    #[arg(long)]
    db: PathBuf,
    /// Snapshot databases to import, e.g. a shell glob `inbox/*/aw.db`. Each
    /// snapshot's device id is its immediate parent directory name.
    #[arg(required = true)]
    snapshots: Vec<PathBuf>,
}

fn main() -> ExitCode {
    let args = Args::parse();
    let datastore = Datastore::new(args.db.to_string_lossy().into_owned(), false);
    let result = import_snapshots(&args.snapshots, &datastore);
    datastore.close();

    match result {
        Ok(summary) => {
            for bucket in &summary.buckets {
                println!(
                    "{} -> {}: {} rows, {} distinct, {} in-window, {} inserted",
                    bucket.device,
                    bucket.dest_bucket,
                    bucket.source_events,
                    bucket.distinct_source,
                    bucket.existing_in_window,
                    bucket.inserted,
                );
            }
            println!("total inserted: {}", summary.total_inserted());
            ExitCode::SUCCESS
        }
        Err(error) => {
            eprintln!("import failed: {error}");
            ExitCode::FAILURE
        }
    }
}
