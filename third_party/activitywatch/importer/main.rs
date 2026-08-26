use std::path::PathBuf;
use std::process::ExitCode;

use aw_client_rust::AwClient;
use aw_importer::import_snapshots;
use clap::Parser;

/// Idempotent one-way importer of per-device ActivityWatch snapshots into a
/// central aw-server over its REST API.
#[derive(Parser)]
struct Args {
    /// Host of the central aw-server to import into.
    #[arg(long)]
    host: String,
    /// Port of the central aw-server.
    #[arg(long, default_value_t = 5600)]
    port: u16,
    /// Snapshot databases to import, e.g. a shell glob `inbox/*/aw.db`. Each
    /// snapshot's device id is its immediate parent directory name.
    #[arg(required = true)]
    snapshots: Vec<PathBuf>,
}

fn main() -> ExitCode {
    let args = Args::parse();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("build tokio runtime");

    runtime.block_on(async {
        let client = match AwClient::new(&args.host, args.port, "aw-importer") {
            Ok(client) => client,
            Err(error) => {
                eprintln!(
                    "could not reach aw-server at {}:{}: {error}",
                    args.host, args.port
                );
                return ExitCode::FAILURE;
            }
        };
        match import_snapshots(&args.snapshots, &client).await {
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
    })
}
