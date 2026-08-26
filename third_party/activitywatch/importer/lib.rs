//! One-way importer that folds per-device ActivityWatch snapshots into a single
//! central aw-server over its REST API.
//!
//! # What it imports
//! Exactly the snapshot databases it is handed — typically a shell/CronJob glob
//! like `inbox/*/aw.db`. Discovery and filtering are the caller's job: which files
//! count as snapshots, and skipping transport artifacts, are decided by the glob,
//! so no code here knows the transport exists. Syncthing, for one, keeps the
//! winning copy at the clean name and renames the loser to `*.sync-conflict-*`,
//! which `*/aw.db` simply does not match; a device with only a conflict copy is
//! not imported that run rather than importing a diverged file.
//!
//! # Writing through the server, not the file
//! The central store is reached only through `aw_client_rust::AwClient` (HTTP),
//! never by opening its SQLite file. aw-server-rust caches its bucket table in
//! memory and assumes it is the sole writer, so a second process writing the file
//! directly would leave that cache incoherent. Going through the API keeps the
//! running aw-server the one writer.
//!
//! # Device identity
//! Each snapshot's device id is its immediate parent directory name — the inbox
//! layout's one carrier of provenance, never the source bucket's own hostname.
//! Two devices whose watchers both report hostname `localhost` therefore land in
//! distinct destination buckets (`<device>::<source-bucket>`) instead of one
//! silently overwriting the other.
//!
//! # Canonicalization rule
//! The central store holds exactly the set of distinct
//! `(device, bucket, starttime, endtime, data)` tuples observed across every
//! import. Within one import duplicate tuples collapse to one; re-importing the
//! same snapshot inserts nothing. `data` is compared as canonical JSON (object
//! keys sorted recursively) so key ordering never splits a tuple.
//!
//! # Read-only sources
//! Snapshots are opened `immutable=1` read-only: no schema migration, no
//! WAL/journal side files, and the source bytes are never touched.

mod snapshot;

use std::collections::HashSet;
use std::path::Path;
use std::path::PathBuf;

use aw_client_rust::AwClient;
use aw_models::Bucket;
use aw_models::BucketMetadata;
use aw_models::Event;
use chrono::DateTime;
use chrono::Duration;
use chrono::Utc;
use serde_json::Map;
use serde_json::Value;

use crate::snapshot::SourceBucket;
use crate::snapshot::SourceEvent;

const NANOS_PER_SEC: i64 = 1_000_000_000;

/// Canonical dedup key: `(starttime_nanos, endtime_nanos, canonical_data_json)`.
/// The device and bucket components of the full canonicalization tuple are carried
/// by the destination bucket id, so this key identifies an event within one bucket.
type EventKey = (i64, i64, String);

#[derive(Debug, thiserror::Error)]
pub enum ImportError {
    #[error("I/O error at {}: {source}", .path.display())]
    Io {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("SQLite error on snapshot {}: {source}", .path.display())]
    Sqlite {
        path: PathBuf,
        source: rusqlite::Error,
    },
    #[error("central aw-server request failed: {0}")]
    Http(#[from] reqwest::Error),
    #[error("corrupt event data in source bucket {bucket}: {source}")]
    CorruptEventData {
        bucket: String,
        source: serde_json::Error,
    },
    #[error("corrupt bucket metadata in snapshot {}: {source}", .path.display())]
    CorruptBucketData {
        path: PathBuf,
        source: serde_json::Error,
    },
    #[error("snapshot path has no parent directory to name its device: {}", .0.display())]
    BadSnapshotPath(PathBuf),
}

/// Per-bucket outcome of an import.
#[derive(Debug)]
pub struct BucketImport {
    pub device: String,
    pub source_bucket: String,
    pub dest_bucket: String,
    pub source_events: usize,
    pub distinct_source: usize,
    pub existing_in_window: usize,
    pub inserted: usize,
}

#[derive(Debug, Default)]
pub struct ImportSummary {
    pub buckets: Vec<BucketImport>,
}

impl ImportSummary {
    pub fn total_inserted(&self) -> usize {
        self.buckets.iter().map(|bucket| bucket.inserted).sum()
    }
}

/// Import each given snapshot database into the central aw-server behind `client`.
/// A snapshot's device id is its immediate parent directory name, so a glob like
/// `inbox/*/aw.db` yields `rugged`, `wyrm2`, … — the caller's glob is what decides
/// which files are snapshots at all.
///
/// Idempotent: re-importing the same snapshot inserts nothing. A failure on one
/// snapshot aborts the run, but because every insert dedups, a re-run after the
/// cause is fixed converges with no double-counting.
pub async fn import_snapshots(
    snapshots: &[PathBuf],
    client: &AwClient,
) -> Result<ImportSummary, ImportError> {
    // One listing of the server's buckets seeds which destinations already exist;
    // buckets this run creates are added as we go, so `create_bucket` is called at
    // most once per destination.
    let mut existing_buckets: HashSet<String> = client.get_buckets().await?.into_keys().collect();
    let mut summary = ImportSummary::default();
    for path in snapshots {
        let device = device_of(path)?;
        import_snapshot(&device, path, client, &mut existing_buckets, &mut summary).await?;
    }
    Ok(summary)
}

/// A snapshot's device id: the name of its immediate parent directory.
fn device_of(path: &Path) -> Result<String, ImportError> {
    path.parent()
        .and_then(Path::file_name)
        .map(|name| name.to_string_lossy().into_owned())
        .filter(|name| !name.is_empty())
        .ok_or_else(|| ImportError::BadSnapshotPath(path.to_path_buf()))
}

async fn import_snapshot(
    device: &str,
    path: &Path,
    client: &AwClient,
    existing_buckets: &mut HashSet<String>,
    summary: &mut ImportSummary,
) -> Result<(), ImportError> {
    let conn = snapshot::open_readonly(path)?;
    for bucket in snapshot::read_buckets(&conn, path)? {
        let dest_bucket = format!("{device}::{}", bucket.name);
        ensure_bucket(client, existing_buckets, &dest_bucket, device, &bucket).await?;

        let source_events = snapshot::read_events(&conn, path, bucket.bid)?;

        // Dedup only needs existing events that could share a source event's exact
        // (start, end) -- all within the source's own time span -- so read that
        // window (see `dedup_window`) instead of the whole bucket, keeping a
        // re-import proportional to the overlap rather than the accumulated history.
        //
        // The REST events read clips returned events to the query window, but that
        // never disturbs dedup: a source event's (start, end) lies strictly inside
        // the 1ns-widened window, so any stored event that could collide with it is
        // strictly inside too and comes back unclipped; the boundary events that do
        // get clipped can't match any source key. So a windowed clipped read finds
        // every true collision and manufactures no false one.
        let existing = match dedup_window(&source_events) {
            Some((start, end)) => {
                client
                    .get_events(&dest_bucket, Some(start), Some(end), None)
                    .await?
            }
            None => Vec::new(),
        };
        let existing_in_window = existing.len();
        let mut seen: HashSet<EventKey> = existing.iter().map(dest_event_key).collect();

        let mut distinct = HashSet::new();
        let mut to_insert = Vec::new();
        for raw in &source_events {
            let data: Map<String, Value> = serde_json::from_str(&raw.data).map_err(|source| {
                ImportError::CorruptEventData {
                    bucket: bucket.name.clone(),
                    source,
                }
            })?;
            let key = (raw.start_nanos, raw.end_nanos, canonical_object(&data));
            distinct.insert(key.clone());
            if seen.insert(key) {
                to_insert.push(build_event(raw.start_nanos, raw.end_nanos, data));
            }
        }
        let inserted = to_insert.len();
        if !to_insert.is_empty() {
            client.insert_events(&dest_bucket, to_insert).await?;
        }

        summary.buckets.push(BucketImport {
            device: device.to_string(),
            source_bucket: bucket.name,
            dest_bucket,
            source_events: source_events.len(),
            distinct_source: distinct.len(),
            existing_in_window,
            inserted,
        });
    }
    Ok(())
}

/// Create the destination bucket if absent, taking its hostname from the device
/// directory rather than the source bucket's own (colliding) hostname. `existing`
/// tracks which destinations the server already holds, so each is created once.
async fn ensure_bucket(
    client: &AwClient,
    existing: &mut HashSet<String>,
    dest_bucket: &str,
    device: &str,
    source: &SourceBucket,
) -> Result<(), ImportError> {
    if existing.contains(dest_bucket) {
        return Ok(());
    }
    let bucket = Bucket {
        bid: None,
        id: dest_bucket.to_string(),
        _type: source.type_.clone(),
        client: source.client.clone(),
        hostname: device.to_string(),
        created: source.created,
        data: source.data.clone(),
        metadata: BucketMetadata::default(),
        events: None,
        last_updated: None,
    };
    client.create_bucket(&bucket).await?;
    existing.insert(dest_bucket.to_string());
    Ok(())
}

fn build_event(start_nanos: i64, end_nanos: i64, data: Map<String, Value>) -> Event {
    Event::new(
        nanos_to_datetime(start_nanos),
        Duration::nanoseconds(end_nanos - start_nanos),
        data,
    )
}

fn nanos_to_datetime(nanos: i64) -> DateTime<Utc> {
    let secs = nanos.div_euclid(NANOS_PER_SEC);
    let subsec = nanos.rem_euclid(NANOS_PER_SEC) as u32;
    DateTime::from_timestamp(secs, subsec).expect("source timestamp within representable range")
}

/// The window of stored events to dedup a source batch against: the batch's
/// `[min start, max end]` span, widened 1ns each side. A stored event can only
/// collide with a source event by sharing its exact `(start, end)`, so it always
/// overlaps this span -- reading the window is enough, and costs the overlap rather
/// than the bucket's whole history. `None` when the batch is empty.
fn dedup_window(events: &[SourceEvent]) -> Option<(DateTime<Utc>, DateTime<Utc>)> {
    let min_start = events.iter().map(|event| event.start_nanos).min()?;
    let max_end = events.iter().map(|event| event.end_nanos).max()?;
    Some((
        nanos_to_datetime(min_start.saturating_sub(1)),
        nanos_to_datetime(max_end.saturating_add(1)),
    ))
}

fn dest_event_key(event: &Event) -> EventKey {
    let start = event
        .timestamp
        .timestamp_nanos_opt()
        .expect("stored event timestamp within representable range");
    let duration = event
        .duration
        .num_nanoseconds()
        .expect("stored event duration within representable range");
    (start, start + duration, canonical_object(&event.data))
}

/// Canonical string for a `data` map. This crate builds `serde_json` without the
/// `preserve_order` feature, so `Value::Object` is a `BTreeMap` and `to_string`
/// already emits object keys in sorted order, recursively — two maps that differ
/// only in key order serialize identically. The window key-reorder case in the
/// import test guards that assumption.
fn canonical_object(map: &Map<String, Value>) -> String {
    serde_json::to_string(map).expect("json object serializes")
}
