use std::process::ExitCode;

use aw_client_rust::AwClient;
use aw_importer::import_device;
use clap::Parser;

/// Idempotent one-way importer of one device's ActivityWatch data into a central
/// aw-server. Reads the device's local aw-server over REST and writes the central
/// one the same way; the source store is only ever read.
#[derive(Parser)]
struct Args {
    /// Host of the source aw-server (the device's own local server).
    #[arg(long, default_value = "127.0.0.1")]
    source_host: String,
    /// Port of the source aw-server.
    #[arg(long, default_value_t = 5600)]
    source_port: u16,
    /// Host of the destination (central) aw-server to import into.
    #[arg(long)]
    dest_host: String,
    /// Port of the destination aw-server.
    #[arg(long, default_value_t = 5600)]
    dest_port: u16,
    /// Device id stamped onto destination buckets as provenance, e.g. `rugged`.
    #[arg(long)]
    device: String,
}

fn main() -> ExitCode {
    let args = Args::parse();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("build tokio runtime");

    runtime.block_on(async {
        let source = match AwClient::new(&args.source_host, args.source_port, "aw-importer-source")
        {
            Ok(client) => client,
            Err(error) => {
                eprintln!(
                    "could not reach source aw-server at {}:{}: {error}",
                    args.source_host, args.source_port
                );
                return ExitCode::FAILURE;
            }
        };
        let dest = match AwClient::new(&args.dest_host, args.dest_port, "aw-importer-dest") {
            Ok(client) => client,
            Err(error) => {
                eprintln!(
                    "could not reach destination aw-server at {}:{}: {error}",
                    args.dest_host, args.dest_port
                );
                return ExitCode::FAILURE;
            }
        };

        match import_device(&source, &dest, &args.device).await {
            Ok(summary) => {
                for bucket in &summary.buckets {
                    println!(
                        "{} -> {}: {} source, {} distinct, {} already in dest, {} inserted",
                        bucket.device,
                        bucket.dest_bucket,
                        bucket.source_events,
                        bucket.distinct_source,
                        bucket.dest_existing,
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
