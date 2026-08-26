use std::path::PathBuf;
use std::process::ExitCode;

use aw_datastore::Datastore;
use aw_importer::import_inbox;
use clap::Parser;

/// Idempotent one-way importer of per-device ActivityWatch snapshots into a
/// central aw-server-rust datastore.
#[derive(Parser)]
struct Args {
    /// Inbox directory: one subdirectory per device, each holding exactly one
    /// immutable snapshot database.
    #[arg(long)]
    inbox: PathBuf,
    /// Central aw-server-rust SQLite datastore to import into (created if absent).
    #[arg(long)]
    db: PathBuf,
}

fn main() -> ExitCode {
    let args = Args::parse();
    let datastore = Datastore::new(args.db.to_string_lossy().into_owned(), false);
    let result = import_inbox(&args.inbox, &datastore);
    datastore.close();

    match result {
        Ok(summary) => {
            for bucket in &summary.buckets {
                println!(
                    "{} -> {}: {} rows, {} distinct, {} existing, {} inserted",
                    bucket.device,
                    bucket.dest_bucket,
                    bucket.source_events,
                    bucket.distinct_source,
                    bucket.existing_before,
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
