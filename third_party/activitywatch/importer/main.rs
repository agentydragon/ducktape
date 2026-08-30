use std::process::ExitCode;

use aw_importer::DEFAULT_RECONCILIATION_LOOKBACK_SECONDS;
use aw_importer::ImportOptions;
use aw_importer::connect;
use aw_importer::import_device_with_options;
use clap::Parser;

/// Idempotent one-way importer of one device's ActivityWatch data into a central
/// aw-server. Reads the device's local aw-server over REST and writes the central
/// one the same way; the source store is only ever read.
///
/// The destination bearer token is read from the `AW_DEST_TOKEN` environment
/// variable, never a flag — a token on the command line would be visible in the
/// process list.
#[derive(Parser)]
struct Args {
    /// URL of the source aw-server (the device's own local server).
    #[arg(long, default_value = "http://127.0.0.1:5600")]
    source_url: String,
    /// URL of the destination (central) aw-server to import into, e.g.
    /// `https://activitywatch-write.allegedly.works`.
    #[arg(long)]
    dest_url: String,
    /// Device id stamped onto destination buckets as provenance, e.g. `rugged`.
    #[arg(long)]
    device: String,
    /// Seconds of recent destination history to reconcile on each run.
    #[arg(long, default_value_t = DEFAULT_RECONCILIATION_LOOKBACK_SECONDS)]
    lookback_seconds: u64,
    /// Scan complete source and destination buckets to recover old gaps.
    /// This is intentionally explicit because it is expensive.
    #[arg(long)]
    full_reconcile: bool,
}

fn main() -> ExitCode {
    let args = Args::parse();
    let dest_token = std::env::var("AW_DEST_TOKEN").ok();

    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("build tokio runtime");

    runtime.block_on(async {
        let source = match connect(&args.source_url, None, "aw-importer-source") {
            Ok(client) => client,
            Err(error) => {
                eprintln!("source: {error}");
                return ExitCode::FAILURE;
            }
        };
        let dest = match connect(&args.dest_url, dest_token, "aw-importer-dest") {
            Ok(client) => client,
            Err(error) => {
                eprintln!("destination: {error}");
                return ExitCode::FAILURE;
            }
        };

        let options = match ImportOptions::from_lookback_seconds(
            args.lookback_seconds,
            args.full_reconcile,
        ) {
            Ok(options) => options,
            Err(error) => {
                eprintln!("options: {error}");
                return ExitCode::FAILURE;
            }
        };

        match import_device_with_options(&source, &dest, &args.device, options).await {
            Ok(summary) => {
                for bucket in &summary.buckets {
                    let mode = if bucket.full_reconcile {
                        "full"
                    } else {
                        "incremental"
                    };
                    let frontier = bucket
                        .reconciliation_frontier
                        .map(|time| time.to_rfc3339())
                        .unwrap_or_else(|| "none".to_string());
                    println!(
                        "{} -> {} ({mode}, frontier {frontier}): {} source, {} distinct, {} scanned in dest, {} inserted",
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
