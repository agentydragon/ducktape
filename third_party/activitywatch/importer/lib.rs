//! One-way importer that folds one device's ActivityWatch data into a central
//! aw-server. It reads the device's own aw-server over the REST API and writes the
//! central one the same way — both ends stay the sole writer of their own SQLite
//! file. This is the correct shape of what upstream `aw-sync` attempted: read-only
//! at the source, provenance from the machine's identity, idempotent on insert.
//!
//! # Writing through the server, not the file
//! Each store is reached only through `aw_client_rust::AwClient` (HTTP), never by
//! opening its SQLite file. aw-server-rust caches its bucket table in memory and
//! assumes it is the sole writer, so a second process writing the file directly
//! would leave that cache incoherent. Going through the API keeps each running
//! aw-server the one writer of its own store.
//!
//! # Device identity
//! The importer runs on the device and is told the device id (`--device`), so
//! provenance comes from the machine, never the source bucket's own hostname. Two
//! devices whose watchers both report hostname `localhost` land in distinct
//! destination buckets (`<device>::<source-bucket>`) instead of one silently
//! overwriting the other.
//!
//! # Canonicalization rule
//! The central store holds exactly the set of distinct
//! `(device, bucket, starttime, endtime, data)` tuples observed across every
//! import. Within one import duplicate tuples collapse to one; re-importing the
//! same source inserts nothing. `data` is compared as canonical JSON (object keys
//! sorted recursively) so key ordering never splits a tuple.
//!
//! # Read-only source
//! The importer only ever issues GETs to the source server — it never writes to
//! the device's store, so a sync cannot corrupt or amplify the source data.

use std::collections::HashSet;

use aw_client_rust::AwClient;
use aw_models::Bucket;
use aw_models::BucketMetadata;
use aw_models::Event;
use chrono::DateTime;
use chrono::TimeDelta;
use chrono::Utc;
use reqwest::Url;
use serde_json::Map;
use serde_json::Value;

/// Canonical dedup key: `(starttime_nanos, endtime_nanos, canonical_data_json)`.
/// The device and bucket components of the full canonicalization tuple are carried
/// by the destination bucket id, so this key identifies an event within one bucket.
type EventKey = (i64, i64, String);

/// Events per insert request. A bucket's new events are POSTed in batches of this
/// size, so a first backfill — a whole busy bucket is ~10 MB — never becomes one
/// oversized request; each batch is a few hundred KB. The steady-state delta is
/// usually a single batch.
pub const INSERT_BATCH_SIZE: usize = 1000;

/// The overlap used to repair a failed or interrupted recent import. The
/// destination's newest event is the cursor, so this is measured backwards from
/// that event rather than from wall-clock time.
pub const DEFAULT_RECONCILIATION_LOOKBACK_SECONDS: u64 = 60 * 60;

/// Controls one importer invocation.
#[derive(Debug, Clone, Copy)]
pub struct ImportOptions {
    /// Re-read this much history before the destination's newest event. The
    /// overlap catches partially completed batches and late updates without
    /// downloading the lifetime history on every run.
    pub lookback: TimeDelta,
    /// Scan complete source and destination buckets. This is an explicit
    /// recovery mode and is never selected by the normal timer invocation.
    pub full_reconcile: bool,
}

impl Default for ImportOptions {
    fn default() -> Self {
        Self {
            lookback: TimeDelta::seconds(DEFAULT_RECONCILIATION_LOOKBACK_SECONDS as i64),
            full_reconcile: false,
        }
    }
}

impl ImportOptions {
    /// Build options from the CLI's unsigned seconds value, rejecting values
    /// that chrono cannot represent instead of wrapping them.
    pub fn from_lookback_seconds(seconds: u64, full_reconcile: bool) -> Result<Self, String> {
        let seconds = i64::try_from(seconds)
            .map_err(|_| "lookback seconds exceed the supported range".to_string())?;
        let lookback = TimeDelta::try_seconds(seconds)
            .ok_or_else(|| "lookback seconds exceed chrono's supported range".to_string())?;
        Ok(Self {
            lookback,
            full_reconcile,
        })
    }
}

#[derive(Debug, thiserror::Error)]
pub enum ImportError {
    #[error("aw-server request failed: {0}")]
    Http(#[from] reqwest::Error),
    #[error("could not connect to aw-server: {0}")]
    Connect(String),
}

/// Build a client for the aw-server at `url`, sending `token` as a bearer on every
/// request when set. aw-client's own constructor only ever builds an `http://host:port`
/// base URL, so we take its bearer wiring via `new_with_api_key` and then point the
/// (public) base URL at `url` — which may be `https` and carry a path — so the
/// central server can sit behind a TLS-terminating, bearer-gated route.
pub fn connect(url: &str, token: Option<String>, name: &str) -> Result<AwClient, ImportError> {
    let parsed =
        Url::parse(url).map_err(|error| ImportError::Connect(format!("{url}: {error}")))?;
    let host = parsed
        .host_str()
        .ok_or_else(|| ImportError::Connect(format!("no host in {url}")))?;
    let port = parsed
        .port_or_known_default()
        .ok_or_else(|| ImportError::Connect(format!("no port in {url}")))?;
    let mut client = AwClient::new_with_api_key(host, port, name, token)
        .map_err(|error| ImportError::Connect(error.to_string()))?;
    client.baseurl = parsed;
    Ok(client)
}

/// Per-bucket outcome of an import.
#[derive(Debug)]
pub struct BucketImport {
    pub device: String,
    pub source_bucket: String,
    pub dest_bucket: String,
    pub source_events: usize,
    pub distinct_source: usize,
    pub dest_existing: usize,
    pub inserted: usize,
    /// The lower bound used for this bucket, or `None` for a full scan or an
    /// empty destination bucket.
    pub reconciliation_frontier: Option<DateTime<Utc>>,
    pub full_reconcile: bool,
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

/// Import every bucket with explicit reconciliation options. Idempotent: a
/// second import of unchanged source data inserts nothing, so a re-run after a
/// transient failure converges with no double-counting.
pub async fn import_device_with_options(
    source: &AwClient,
    dest: &AwClient,
    device: &str,
    options: ImportOptions,
) -> Result<ImportSummary, ImportError> {
    // One listing of the destination's buckets seeds which destinations already
    // exist; buckets this run creates are added as we go, so `create_bucket` is
    // called at most once per destination.
    let mut existing_buckets: HashSet<String> = dest.get_buckets().await?.into_keys().collect();
    let source_buckets = source.get_buckets().await?;
    // Stable order so the summary and any inserts are deterministic across runs.
    let mut names: Vec<&String> = source_buckets.keys().collect();
    names.sort();

    let mut summary = ImportSummary::default();
    for name in names {
        import_bucket(
            source,
            dest,
            device,
            name,
            &source_buckets[name],
            &mut existing_buckets,
            &mut summary,
            options,
        )
        .await?;
    }
    Ok(summary)
}

async fn import_bucket(
    source: &AwClient,
    dest: &AwClient,
    device: &str,
    source_bucket: &str,
    source_meta: &Bucket,
    existing_buckets: &mut HashSet<String>,
    summary: &mut ImportSummary,
    options: ImportOptions,
) -> Result<(), ImportError> {
    let dest_bucket = format!("{device}::{source_bucket}");
    ensure_bucket(dest, existing_buckets, &dest_bucket, device, source_meta).await?;

    // The destination is the durable cursor. A failed batch cannot make a local
    // checkpoint lie about what the central server received, and an empty bucket
    // naturally triggers the one-time backfill.
    let destination_latest = if options.full_reconcile {
        None
    } else {
        dest.get_events(&dest_bucket, None, None, Some(1))
            .await?
            .into_iter()
            .next()
    };
    let frontier = destination_latest.map(|event| event.timestamp - options.lookback);

    // aw-server clips an event that intersects a requested `start` to that
    // boundary. Pad the request by one nanosecond and discard events before the
    // real frontier, so every event we reconcile retains its original timestamp
    // and duration. There is no end bound, hence no end clipping.
    let query_start = frontier.map(|time| time - TimeDelta::nanoseconds(1));
    let source_events = source
        .get_events(source_bucket, query_start, None, None)
        .await?
        .into_iter()
        .filter(|event| frontier.is_none_or(|time| event.timestamp >= time))
        .collect::<Vec<_>>();

    let existing = if options.full_reconcile || frontier.is_some() {
        dest.get_events(&dest_bucket, query_start, None, None)
            .await?
            .into_iter()
            .filter(|event| frontier.is_none_or(|time| event.timestamp >= time))
            .collect::<Vec<_>>()
    } else {
        // A newly-created or empty destination bucket has no existing events;
        // avoid a redundant full response just to discover that fact.
        Vec::new()
    };
    let dest_existing = existing.len();
    let mut seen: HashSet<EventKey> = existing.iter().map(event_key).collect();

    let mut distinct = HashSet::new();
    let mut to_insert = Vec::new();
    for event in &source_events {
        let key = event_key(event);
        distinct.insert(key.clone());
        if seen.insert(key) {
            to_insert.push(reidentified(event));
        }
    }
    // get_events returns newest-first. Insert oldest-first so a failed batch
    // leaves the destination's newest event at the last contiguous success; the
    // next invocation's cursor and overlap then resume without skipping a tail.
    to_insert.sort_by_key(event_key);
    let inserted = to_insert.len();
    // Batch the inserts so one bucket's backfill is many bounded POSTs, not a
    // single multi-megabyte request. chunks() over an empty vec is a no-op.
    for batch in to_insert.chunks(INSERT_BATCH_SIZE) {
        dest.insert_events(&dest_bucket, batch.to_vec()).await?;
    }

    summary.buckets.push(BucketImport {
        device: device.to_string(),
        source_bucket: source_bucket.to_string(),
        dest_bucket,
        source_events: source_events.len(),
        distinct_source: distinct.len(),
        dest_existing,
        inserted,
        reconciliation_frontier: frontier,
        full_reconcile: options.full_reconcile,
    });
    Ok(())
}

/// Create the destination bucket if absent, stamping its hostname with the device
/// id rather than the source bucket's own (colliding) hostname. `existing` tracks
/// which destinations the server already holds, so each is created once.
async fn ensure_bucket(
    dest: &AwClient,
    existing: &mut HashSet<String>,
    dest_bucket: &str,
    device: &str,
    source: &Bucket,
) -> Result<(), ImportError> {
    if existing.contains(dest_bucket) {
        return Ok(());
    }
    let bucket = Bucket {
        bid: None,
        id: dest_bucket.to_string(),
        _type: source._type.clone(),
        client: source.client.clone(),
        hostname: device.to_string(),
        created: source.created,
        data: source.data.clone(),
        metadata: BucketMetadata::default(),
        events: None,
        last_updated: None,
    };
    dest.create_bucket(&bucket).await?;
    existing.insert(dest_bucket.to_string());
    Ok(())
}

/// A copy of `event` with the source's event id dropped, so the destination server
/// assigns its own id on insert instead of the importer pinning the source's.
fn reidentified(event: &Event) -> Event {
    Event::new(event.timestamp, event.duration, event.data.clone())
}

fn event_key(event: &Event) -> EventKey {
    let start = event
        .timestamp
        .timestamp_nanos_opt()
        .expect("event timestamp within representable range");
    let duration = event
        .duration
        .num_nanoseconds()
        .expect("event duration within representable range");
    (start, start + duration, canonical_object(&event.data))
}

/// Canonical string for a `data` map. This crate builds `serde_json` without the
/// `preserve_order` feature, so `Value::Object` is a `BTreeMap` and `to_string`
/// already emits object keys in sorted order, recursively — two maps that differ
/// only in key order serialize identically. The key-reorder case in the import test
/// guards that assumption.
fn canonical_object(map: &Map<String, Value>) -> String {
    serde_json::to_string(map).expect("json object serializes")
}
