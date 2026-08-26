//! One-way importer that folds per-device ActivityWatch snapshots into a single
//! central aw-server-rust datastore.
//!
//! # Device identity
//! Provenance comes from the inbox layout, never from bucket metadata. The inbox
//! holds one subdirectory per device and that directory name is the device id.
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
//!
//! # Fail closed
//! A Syncthing conflict copy, an ambiguous set of snapshot candidates, or any
//! other unexpected entry in a device directory aborts the run before anything is
//! written to the central store.

mod snapshot;

use std::collections::HashSet;
use std::path::Path;
use std::path::PathBuf;

use aw_datastore::Datastore;
use aw_datastore::DatastoreError;
use aw_models::Bucket;
use aw_models::BucketMetadata;
use aw_models::Event;
use chrono::DateTime;
use chrono::Duration;
use serde_json::Map;
use serde_json::Value;

use crate::snapshot::SourceBucket;

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
    #[error("central datastore error: {0:?}")]
    Datastore(DatastoreError),
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
    #[error("Syncthing conflict file present, refusing to import: {}", .0.display())]
    ConflictFile(PathBuf),
    #[error("device {device} has multiple snapshot candidates, expected one: {candidates:?}")]
    AmbiguousSnapshot {
        device: String,
        candidates: Vec<PathBuf>,
    },
    #[error("unexpected entry in device inbox, refusing to import: {}", .0.display())]
    UnexpectedEntry(PathBuf),
}

impl From<DatastoreError> for ImportError {
    fn from(source: DatastoreError) -> Self {
        ImportError::Datastore(source)
    }
}

/// One device's immutable snapshot, located by inbox layout.
pub struct DeviceSnapshot {
    pub device: String,
    pub path: PathBuf,
}

/// Per-bucket outcome of an import.
#[derive(Debug)]
pub struct BucketImport {
    pub device: String,
    pub source_bucket: String,
    pub dest_bucket: String,
    pub source_events: usize,
    pub distinct_source: usize,
    pub existing_before: usize,
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

/// Import every device snapshot found under `inbox` into `datastore`.
///
/// The inbox is fully validated up front: any conflict, ambiguity, or unexpected
/// entry aborts before the first write, so a failed run leaves the central store
/// untouched.
pub fn import_inbox(inbox: &Path, datastore: &Datastore) -> Result<ImportSummary, ImportError> {
    let snapshots = scan_inbox(inbox)?;
    let mut summary = ImportSummary::default();
    for snapshot in &snapshots {
        import_snapshot(snapshot, datastore, &mut summary)?;
    }
    Ok(summary)
}

/// Resolve the inbox to one snapshot per device, failing closed on anything the
/// layout does not permit.
pub fn scan_inbox(inbox: &Path) -> Result<Vec<DeviceSnapshot>, ImportError> {
    let mut snapshots = Vec::new();
    for device_dir in read_dir_sorted(inbox)? {
        if !device_dir.is_dir() {
            continue;
        }
        let device = file_name(&device_dir);
        if device.starts_with('.') {
            continue;
        }
        if let Some(path) = device_snapshot(&device_dir)? {
            snapshots.push(DeviceSnapshot { device, path });
        }
    }
    Ok(snapshots)
}

/// The single snapshot file in one device directory, or `None` if the device has
/// not exported yet. Dotfiles (Syncthing's `.stfolder`, `.stignore`, ...) are
/// ignored; everything else must be exactly one snapshot database.
fn device_snapshot(dir: &Path) -> Result<Option<PathBuf>, ImportError> {
    let mut candidates = Vec::new();
    for entry in read_dir_sorted(dir)? {
        let name = file_name(&entry);
        if name.starts_with('.') {
            continue;
        }
        if name.contains(".sync-conflict-") {
            return Err(ImportError::ConflictFile(entry));
        }
        if entry.is_dir() || !is_snapshot_db(&name) {
            return Err(ImportError::UnexpectedEntry(entry));
        }
        candidates.push(entry);
    }
    match candidates.len() {
        0 => Ok(None),
        1 => Ok(Some(candidates.pop().unwrap())),
        _ => Err(ImportError::AmbiguousSnapshot {
            device: file_name(dir),
            candidates,
        }),
    }
}

fn is_snapshot_db(name: &str) -> bool {
    let lower = name.to_ascii_lowercase();
    lower.ends_with(".db") || lower.ends_with(".sqlite") || lower.ends_with(".sqlite3")
}

fn import_snapshot(
    snapshot: &DeviceSnapshot,
    datastore: &Datastore,
    summary: &mut ImportSummary,
) -> Result<(), ImportError> {
    let conn = snapshot::open_readonly(&snapshot.path)?;
    for bucket in snapshot::read_buckets(&conn, &snapshot.path)? {
        let dest_bucket = format!("{}::{}", snapshot.device, bucket.name);
        ensure_bucket(datastore, &dest_bucket, &snapshot.device, &bucket)?;

        let existing = datastore.get_events_unclipped(&dest_bucket, None, None, None)?;
        let existing_before = existing.len();
        let mut seen: HashSet<EventKey> = existing.iter().map(dest_event_key).collect();

        let source_events = snapshot::read_events(&conn, &snapshot.path, bucket.bid)?;
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
        if !to_insert.is_empty() {
            datastore.insert_events(&dest_bucket, &to_insert)?;
        }

        summary.buckets.push(BucketImport {
            device: snapshot.device.clone(),
            source_bucket: bucket.name,
            dest_bucket,
            source_events: source_events.len(),
            distinct_source: distinct.len(),
            existing_before,
            inserted: to_insert.len(),
        });
    }
    Ok(())
}

/// Create the destination bucket if absent, taking its hostname from the device
/// directory rather than the source bucket's own (colliding) hostname.
fn ensure_bucket(
    datastore: &Datastore,
    dest_bucket: &str,
    device: &str,
    source: &SourceBucket,
) -> Result<(), ImportError> {
    match datastore.get_bucket(dest_bucket) {
        Ok(_) => Ok(()),
        Err(DatastoreError::NoSuchBucket(_)) => {
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
            match datastore.create_bucket(&bucket) {
                Ok(()) | Err(DatastoreError::BucketAlreadyExists(_)) => Ok(()),
                Err(source) => Err(ImportError::Datastore(source)),
            }
        }
        Err(source) => Err(ImportError::Datastore(source)),
    }
}

fn build_event(start_nanos: i64, end_nanos: i64, data: Map<String, Value>) -> Event {
    let secs = start_nanos.div_euclid(NANOS_PER_SEC);
    let subsec = start_nanos.rem_euclid(NANOS_PER_SEC) as u32;
    let timestamp = DateTime::from_timestamp(secs, subsec).expect("source timestamp within range");
    Event::new(
        timestamp,
        Duration::nanoseconds(end_nanos - start_nanos),
        data,
    )
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
/// idempotency test guards that assumption.
fn canonical_object(map: &Map<String, Value>) -> String {
    serde_json::to_string(map).expect("json object serializes")
}

fn read_dir_sorted(dir: &Path) -> Result<Vec<PathBuf>, ImportError> {
    let mut paths = Vec::new();
    let entries = std::fs::read_dir(dir).map_err(|source| ImportError::Io {
        path: dir.to_path_buf(),
        source,
    })?;
    for entry in entries {
        let entry = entry.map_err(|source| ImportError::Io {
            path: dir.to_path_buf(),
            source,
        })?;
        paths.push(entry.path());
    }
    paths.sort();
    Ok(paths)
}

fn file_name(path: &Path) -> String {
    path.file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default()
}
