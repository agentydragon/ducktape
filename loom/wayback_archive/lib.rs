use std::collections::{HashMap, HashSet, VecDeque};
use std::fmt::{Display, Formatter, Write};
use std::sync::{Arc, Mutex as StdMutex, MutexGuard as StdMutexGuard};
use std::time::{Duration, Instant};

use anyhow::{Context, Result, anyhow};
use async_trait::async_trait;
use bytes::Bytes;
use http::{HeaderMap, StatusCode};
use log::warn;
use object_store::aws::AmazonS3Builder;
use object_store::path::Path as ObjectPath;
use object_store::{ObjectStore, ObjectStoreExt, PutPayload};
use prometheus::{
    Encoder, GaugeVec, HistogramOpts, HistogramVec, IntCounterVec, Opts, Registry, TextEncoder,
};
use sea_orm::entity::prelude::*;
use sea_orm::sea_query::OnConflict;
use sea_orm::{
    ColumnTrait, Condition, ConnectionTrait, Database, DatabaseConnection, DbBackend, EntityTrait,
    QueryFilter, Schema, Set,
};
use serde_json::Value;
use sha2::{Digest, Sha256};
use tokio::sync::{Mutex, Notify};
use tokio::time::timeout;

pub const DEFAULT_MAX_QUEUE_WAIT: Duration = Duration::from_secs(60);
pub const DEFAULT_MAX_BODY_BYTES: usize = 10 * 1024 * 1024;
pub const DEFAULT_MAX_METADATA_BYTES: usize = 10 * 1024 * 1024;
pub const DEFAULT_AVAILABILITY_TIMEOUT: Duration = Duration::from_secs(15);
pub const DEFAULT_CDX_TIMEOUT: Duration = Duration::from_secs(45);
pub const DEFAULT_REPLAY_TIMEOUT: Duration = Duration::from_secs(60);

const RETRY_AFTER_SECONDS: u64 = 30;
const HEALTH_WINDOW: Duration = Duration::from_secs(30);
const HEALTH_MIN_SAMPLES: usize = 10;
const DURATION_BUCKETS: [f64; 16] = [
    0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 45.0, 60.0, 90.0, 120.0, 180.0, 300.0,
];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Endpoint {
    Availability,
    Cdx,
    Replay,
}

impl Endpoint {
    const ALL: [Endpoint; 3] = [Endpoint::Availability, Endpoint::Cdx, Endpoint::Replay];

    fn retry_after_seconds(self) -> u64 {
        match self {
            Endpoint::Availability => 5,
            Endpoint::Cdx | Endpoint::Replay => RETRY_AFTER_SECONDS,
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Endpoint::Availability => "availability",
            Endpoint::Cdx => "cdx",
            Endpoint::Replay => "replay",
        }
    }

    fn metadata_path(self) -> Option<&'static str> {
        match self {
            Endpoint::Availability => Some("/wayback/available"),
            Endpoint::Cdx => Some("/cdx/search/cdx"),
            Endpoint::Replay => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
enum AcquisitionFailureReason {
    SingleFlightQueueTimeout,
    LimiterQueueTimeout,
    UpstreamRetryAfter,
    UpstreamTransientStatus,
    UpstreamFetchTimeout,
    UpstreamFetchError,
}

impl AcquisitionFailureReason {
    fn as_str(self) -> &'static str {
        match self {
            AcquisitionFailureReason::SingleFlightQueueTimeout => "single_flight_queue_timeout",
            AcquisitionFailureReason::LimiterQueueTimeout => "limiter_queue_timeout",
            AcquisitionFailureReason::UpstreamRetryAfter => "upstream_retry_after",
            AcquisitionFailureReason::UpstreamTransientStatus => "upstream_transient_status",
            AcquisitionFailureReason::UpstreamFetchTimeout => "upstream_fetch_timeout",
            AcquisitionFailureReason::UpstreamFetchError => "upstream_fetch_error",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
enum UpstreamFetchResult {
    Stored,
    BodyTooLarge,
    UpstreamRetryAfter,
    UpstreamTransientStatus,
    UpstreamFetchTimeout,
    UpstreamFetchError,
}

impl UpstreamFetchResult {
    fn outcome(self) -> &'static str {
        match self {
            UpstreamFetchResult::Stored | UpstreamFetchResult::BodyTooLarge => "success",
            UpstreamFetchResult::UpstreamRetryAfter
            | UpstreamFetchResult::UpstreamTransientStatus
            | UpstreamFetchResult::UpstreamFetchTimeout
            | UpstreamFetchResult::UpstreamFetchError => "failure",
        }
    }

    fn reason(self) -> &'static str {
        match self {
            UpstreamFetchResult::Stored => "stored",
            UpstreamFetchResult::BodyTooLarge => "body_too_large",
            UpstreamFetchResult::UpstreamRetryAfter => {
                AcquisitionFailureReason::UpstreamRetryAfter.as_str()
            }
            UpstreamFetchResult::UpstreamTransientStatus => {
                AcquisitionFailureReason::UpstreamTransientStatus.as_str()
            }
            UpstreamFetchResult::UpstreamFetchTimeout => {
                AcquisitionFailureReason::UpstreamFetchTimeout.as_str()
            }
            UpstreamFetchResult::UpstreamFetchError => {
                AcquisitionFailureReason::UpstreamFetchError.as_str()
            }
        }
    }
}

impl UpstreamFetchResult {
    fn from_upstream_failure(reason: AcquisitionFailureReason) -> Self {
        match reason {
            AcquisitionFailureReason::UpstreamRetryAfter => UpstreamFetchResult::UpstreamRetryAfter,
            AcquisitionFailureReason::UpstreamTransientStatus => {
                UpstreamFetchResult::UpstreamTransientStatus
            }
            AcquisitionFailureReason::UpstreamFetchTimeout => {
                UpstreamFetchResult::UpstreamFetchTimeout
            }
            AcquisitionFailureReason::UpstreamFetchError => UpstreamFetchResult::UpstreamFetchError,
            AcquisitionFailureReason::SingleFlightQueueTimeout
            | AcquisitionFailureReason::LimiterQueueTimeout => unreachable!(
                "queue failures happen before upstream fetches and have no fetch duration"
            ),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
enum LimiterWaitOutcome {
    Acquired,
    Timeout,
}

impl LimiterWaitOutcome {
    fn as_str(self) -> &'static str {
        match self {
            LimiterWaitOutcome::Acquired => "acquired",
            LimiterWaitOutcome::Timeout => "timeout",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
enum SingleFlightWaitOutcome {
    Owner,
    FilledByPeer,
    Timeout,
}

impl SingleFlightWaitOutcome {
    fn as_str(self) -> &'static str {
        match self {
            SingleFlightWaitOutcome::Owner => "owner",
            SingleFlightWaitOutcome::FilledByPeer => "filled_by_peer",
            SingleFlightWaitOutcome::Timeout => "timeout",
        }
    }
}

#[derive(Debug)]
struct ArchiveMetrics {
    acquisition_attempts: IntCounterVec,
    acquisition_failures: IntCounterVec,
    upstream_fetch_duration: HistogramVec,
    limiter_wait_duration: HistogramVec,
    single_flight_wait_duration: HistogramVec,
    single_flight_waiters: StdMutex<HashMap<Endpoint, usize>>,
}

impl Default for ArchiveMetrics {
    fn default() -> Self {
        Self {
            acquisition_attempts: IntCounterVec::new(
                Opts::new(
                    "wayback_archive_acquisition_attempts_total",
                    "Archive miss acquisitions attempted against IA.",
                ),
                &["endpoint"],
            )
            .expect("acquisition_attempts metric definition is valid"),
            acquisition_failures: IntCounterVec::new(
                Opts::new(
                    "wayback_archive_acquisition_failures_total",
                    "Archive miss acquisitions that returned a retryable/backpressure response before storing a result.",
                ),
                &["endpoint", "reason", "status"],
            )
            .expect("acquisition_failures metric definition is valid"),
            upstream_fetch_duration: HistogramVec::new(
                HistogramOpts::new(
                    "wayback_archive_upstream_fetch_duration_seconds",
                    "Duration of upstream IA fetches after acquiring an endpoint limiter slot.",
                )
                .buckets(DURATION_BUCKETS.to_vec()),
                &["archive_endpoint", "outcome", "reason", "status"],
            )
            .expect("upstream_fetch_duration metric definition is valid"),
            limiter_wait_duration: HistogramVec::new(
                HistogramOpts::new(
                    "wayback_archive_limiter_wait_duration_seconds",
                    "Duration spent waiting for endpoint limiter capacity or backoff.",
                )
                .buckets(DURATION_BUCKETS.to_vec()),
                &["archive_endpoint", "outcome"],
            )
            .expect("limiter_wait_duration metric definition is valid"),
            single_flight_wait_duration: HistogramVec::new(
                HistogramOpts::new(
                    "wayback_archive_single_flight_wait_duration_seconds",
                    "Duration spent waiting for an identical archive fill already in flight.",
                )
                .buckets(DURATION_BUCKETS.to_vec()),
                &["archive_endpoint", "outcome"],
            )
            .expect("single_flight_wait_duration metric definition is valid"),
            single_flight_waiters: StdMutex::new(HashMap::new()),
        }
    }
}

impl ArchiveMetrics {
    fn record_acquisition_attempt(&self, endpoint: Endpoint) {
        self.acquisition_attempts
            .with_label_values(&[endpoint.as_str()])
            .inc();
    }

    fn record_acquisition_failure(
        &self,
        endpoint: Endpoint,
        reason: AcquisitionFailureReason,
        status: Option<u16>,
    ) {
        self.acquisition_failures
            .with_label_values(&[endpoint.as_str(), reason.as_str(), &status_label(status)])
            .inc();
    }

    fn record_upstream_fetch_duration(
        &self,
        endpoint: Endpoint,
        result: UpstreamFetchResult,
        status: Option<u16>,
        duration: Duration,
    ) {
        self.upstream_fetch_duration
            .with_label_values(&[
                endpoint.as_str(),
                result.outcome(),
                result.reason(),
                &status_label(status),
            ])
            .observe(duration.as_secs_f64());
    }

    fn record_limiter_wait_duration(
        &self,
        endpoint: Endpoint,
        outcome: LimiterWaitOutcome,
        duration: Duration,
    ) {
        self.limiter_wait_duration
            .with_label_values(&[endpoint.as_str(), outcome.as_str()])
            .observe(duration.as_secs_f64());
    }

    fn record_single_flight_wait_duration(
        &self,
        endpoint: Endpoint,
        outcome: SingleFlightWaitOutcome,
        duration: Duration,
    ) {
        self.single_flight_wait_duration
            .with_label_values(&[endpoint.as_str(), outcome.as_str()])
            .observe(duration.as_secs_f64());
    }

    fn begin_single_flight_wait(self: &Arc<Self>, endpoint: Endpoint) -> SingleFlightWaitGuard {
        {
            let mut waiters = self
                .single_flight_waiters
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            *waiters.entry(endpoint).or_default() += 1;
        }
        SingleFlightWaitGuard {
            metrics: Arc::clone(self),
            endpoint,
        }
    }

    fn finish_single_flight_wait(&self, endpoint: Endpoint) {
        let mut waiters = self
            .single_flight_waiters
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if let Some(count) = waiters.get_mut(&endpoint) {
            *count = count.saturating_sub(1);
        }
    }

    fn single_flight_waiter_counts(&self) -> Vec<(Endpoint, usize)> {
        self.single_flight_waiters
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .iter()
            .map(|(endpoint, count)| (*endpoint, *count))
            .collect()
    }
}

#[derive(Debug)]
struct SingleFlightWaitGuard {
    metrics: Arc<ArchiveMetrics>,
    endpoint: Endpoint,
}

impl Drop for SingleFlightWaitGuard {
    fn drop(&mut self) {
        self.metrics.finish_single_flight_wait(self.endpoint);
    }
}

fn status_label(status: Option<u16>) -> String {
    status
        .map(|status| status.to_string())
        .unwrap_or_else(|| "none".to_string())
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct ReplayKey {
    pub capture_ts: String,
    pub modifier: String,
    pub original_url: String,
}

impl ReplayKey {
    fn ia_path(&self) -> String {
        format!(
            "/web/{}{}/{}",
            self.capture_ts, self.modifier, self.original_url
        )
    }
}

impl Display for ReplayKey {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "{}{}:{}",
            self.capture_ts, self.modifier, self.original_url
        )
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StoredReplay {
    Capture(ReplayRecord),
    BodyTooLarge {
        key: ReplayKey,
        observed_size: usize,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReplayRecord {
    pub key: ReplayKey,
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Bytes,
    pub blob_key: Option<String>,
    pub sha256: String,
    pub body_size: usize,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct MetadataKey {
    pub endpoint: Endpoint,
    pub normalized_query: String,
}

impl Display for MetadataKey {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}?{}", self.endpoint.as_str(), self.normalized_query)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MetadataRequest {
    pub key: MetadataKey,
    pub raw_query: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StoredMetadata {
    Response(MetadataRecord),
    BodyTooLarge {
        key: MetadataKey,
        observed_size: usize,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MetadataRecord {
    pub key: MetadataKey,
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Bytes,
    pub sha256: String,
    pub body_size: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ArchiveResponse {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Bytes,
}

impl ArchiveResponse {
    fn text(status: StatusCode, text: impl Into<String>) -> Self {
        Self {
            status: status.as_u16(),
            headers: vec![(
                "content-type".to_string(),
                "text/plain; charset=utf-8".to_string(),
            )],
            body: Bytes::from(text.into()),
        }
    }

    fn retry_after(endpoint: Endpoint) -> Self {
        Self::retry_after_duration(endpoint, None)
    }

    fn retry_after_duration(endpoint: Endpoint, duration: Option<Duration>) -> Self {
        Self {
            status: StatusCode::SERVICE_UNAVAILABLE.as_u16(),
            headers: vec![
                (
                    "content-type".to_string(),
                    "text/plain; charset=utf-8".to_string(),
                ),
                (
                    "retry-after".to_string(),
                    retry_after_seconds(duration)
                        .unwrap_or_else(|| endpoint.retry_after_seconds())
                        .to_string(),
                ),
            ],
            body: Bytes::from("archive acquisition is backing off\n"),
        }
    }
}

#[derive(Debug, Clone)]
pub struct UpstreamResponse {
    pub status: u16,
    pub headers: HeaderMap,
    pub body: Bytes,
}

#[async_trait]
pub trait ArchiveStore: Send + Sync {
    async fn get_replay(&self, key: &ReplayKey) -> Result<Option<StoredReplay>>;
    async fn put_replay(&self, replay: StoredReplay) -> Result<()>;
    async fn get_metadata(&self, key: &MetadataKey) -> Result<Option<StoredMetadata>>;
    async fn put_metadata(&self, metadata: StoredMetadata) -> Result<()>;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BlobRef {
    pub key: String,
    pub sha256: String,
    pub size: usize,
}

#[async_trait]
pub trait BlobStore: Send + Sync {
    async fn put_body(&self, body: Bytes) -> Result<BlobRef>;
    async fn get_body(&self, key: &str) -> Result<Bytes>;
}

#[async_trait]
pub trait IaClient: Send + Sync {
    async fn fetch_replay(
        &self,
        key: &ReplayKey,
        max_body_bytes: usize,
    ) -> Result<UpstreamResponse>;
    async fn fetch_metadata(
        &self,
        request: &MetadataRequest,
        max_body_bytes: usize,
    ) -> Result<UpstreamResponse>;
}

#[derive(Default)]
pub struct MemoryArchiveStore {
    replays: Mutex<HashMap<ReplayKey, StoredReplay>>,
    metadata: Mutex<HashMap<MetadataKey, StoredMetadata>>,
}

impl MemoryArchiveStore {
    pub fn new() -> Self {
        Self::default()
    }
}

#[async_trait]
impl ArchiveStore for MemoryArchiveStore {
    async fn get_replay(&self, key: &ReplayKey) -> Result<Option<StoredReplay>> {
        Ok(self.replays.lock().await.get(key).cloned())
    }

    async fn put_replay(&self, replay: StoredReplay) -> Result<()> {
        let key = match &replay {
            StoredReplay::Capture(record) => record.key.clone(),
            StoredReplay::BodyTooLarge { key, .. } => key.clone(),
        };
        self.replays.lock().await.insert(key, replay);
        Ok(())
    }

    async fn get_metadata(&self, key: &MetadataKey) -> Result<Option<StoredMetadata>> {
        Ok(self.metadata.lock().await.get(key).cloned())
    }

    async fn put_metadata(&self, metadata: StoredMetadata) -> Result<()> {
        let key = match &metadata {
            StoredMetadata::Response(record) => record.key.clone(),
            StoredMetadata::BodyTooLarge { key, .. } => key.clone(),
        };
        self.metadata.lock().await.insert(key, metadata);
        Ok(())
    }
}

#[derive(Default)]
pub struct MemoryBlobStore {
    blobs: Mutex<HashMap<String, Bytes>>,
}

impl MemoryBlobStore {
    pub fn new() -> Self {
        Self::default()
    }
}

#[async_trait]
impl BlobStore for MemoryBlobStore {
    async fn put_body(&self, body: Bytes) -> Result<BlobRef> {
        let sha256 = sha256_hex(&body);
        let key = blob_key(&sha256);
        self.blobs.lock().await.insert(key.clone(), body.clone());
        Ok(BlobRef {
            key,
            sha256,
            size: body.len(),
        })
    }

    async fn get_body(&self, key: &str) -> Result<Bytes> {
        self.blobs
            .lock()
            .await
            .get(key)
            .cloned()
            .ok_or_else(|| anyhow!("blob missing: {key}"))
    }
}

pub struct S3BlobStore {
    store: Arc<dyn ObjectStore>,
}

impl S3BlobStore {
    pub fn new(
        endpoint: String,
        bucket: String,
        access_key_id: String,
        secret_access_key: String,
        region: String,
        allow_http: bool,
    ) -> Result<Self> {
        let store = AmazonS3Builder::new()
            .with_endpoint(endpoint)
            .with_bucket_name(bucket)
            .with_access_key_id(access_key_id)
            .with_secret_access_key(secret_access_key)
            .with_region(region)
            .with_allow_http(allow_http)
            .with_virtual_hosted_style_request(false)
            .build()?;
        Ok(Self {
            store: Arc::new(store),
        })
    }

    pub fn from_env() -> Result<Self> {
        Self::new(
            required_env("WAYBACK_ARCHIVE_S3_ENDPOINT")?,
            required_env("WAYBACK_ARCHIVE_S3_BUCKET")?,
            required_env("WAYBACK_ARCHIVE_S3_ACCESS_KEY_ID")?,
            required_env("WAYBACK_ARCHIVE_S3_SECRET_ACCESS_KEY")?,
            std::env::var("WAYBACK_ARCHIVE_S3_REGION").unwrap_or_else(|_| "us-east-1".to_string()),
            std::env::var("WAYBACK_ARCHIVE_S3_ALLOW_HTTP")
                .map_or(true, |value| value == "1" || value == "true"),
        )
    }
}

#[async_trait]
impl BlobStore for S3BlobStore {
    async fn put_body(&self, body: Bytes) -> Result<BlobRef> {
        let sha256 = sha256_hex(&body);
        let key = blob_key(&sha256);
        self.store
            .put(
                &ObjectPath::from(key.as_str()),
                PutPayload::from_bytes(body.clone()),
            )
            .await
            .with_context(|| format!("putting replay blob {key}"))?;
        Ok(BlobRef {
            key,
            sha256,
            size: body.len(),
        })
    }

    async fn get_body(&self, key: &str) -> Result<Bytes> {
        Ok(self
            .store
            .get(&ObjectPath::from(key))
            .await
            .with_context(|| format!("getting replay blob {key}"))?
            .bytes()
            .await?)
    }
}

fn required_env(name: &str) -> Result<String> {
    std::env::var(name).with_context(|| format!("{name} must be set"))
}

fn sha256_hex(body: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(body);
    let digest = hasher.finalize();
    let mut encoded = String::with_capacity(64);
    for byte in digest {
        write!(&mut encoded, "{byte:02x}").expect("writing to String should not fail");
    }
    encoded
}

fn blob_key(sha256: &str) -> String {
    format!("sha256/{}/{}/{}", &sha256[..2], &sha256[2..4], sha256)
}

mod replay_record {
    use sea_orm::entity::prelude::*;

    #[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
    #[sea_orm(table_name = "wayback_replay_records")]
    pub struct Model {
        #[sea_orm(primary_key, auto_increment = false)]
        pub capture_ts: String,
        #[sea_orm(primary_key, auto_increment = false)]
        pub modifier: String,
        #[sea_orm(primary_key, auto_increment = false)]
        pub canonical_original_url: String,
        pub status: Option<i32>,
        pub headers: Json,
        pub blob_key: Option<String>,
        pub sha256: Option<String>,
        pub body_size: Option<i64>,
        pub classification: String,
        pub observed_size: Option<i64>,
    }

    #[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
    pub enum Relation {}

    impl ActiveModelBehavior for ActiveModel {}
}

mod metadata_record {
    use sea_orm::entity::prelude::*;

    #[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
    #[sea_orm(table_name = "wayback_metadata_records")]
    pub struct Model {
        #[sea_orm(primary_key, auto_increment = false)]
        pub endpoint: String,
        #[sea_orm(primary_key, auto_increment = false)]
        pub normalized_query: String,
        pub status: Option<i32>,
        pub headers: Json,
        pub body: Option<Vec<u8>>,
        pub sha256: Option<String>,
        pub body_size: Option<i64>,
        pub classification: String,
        pub observed_size: Option<i64>,
    }

    #[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
    pub enum Relation {}

    impl ActiveModelBehavior for ActiveModel {}
}

pub struct PostgresArchiveStore {
    db: DatabaseConnection,
    blobs: Arc<dyn BlobStore>,
}

impl PostgresArchiveStore {
    pub async fn new(database_url: String, blobs: Arc<dyn BlobStore>) -> Result<Self> {
        let db = Database::connect(database_url).await?;
        let store = Self { db, blobs };
        store.migrate().await?;
        Ok(store)
    }

    async fn migrate(&self) -> Result<()> {
        let schema = Schema::new(DbBackend::Postgres);
        let create_table = schema
            .create_table_from_entity(replay_record::Entity)
            .if_not_exists()
            .to_owned();
        self.db
            .execute(self.db.get_database_backend().build(&create_table))
            .await?;
        let create_table = schema
            .create_table_from_entity(metadata_record::Entity)
            .if_not_exists()
            .to_owned();
        self.db
            .execute(self.db.get_database_backend().build(&create_table))
            .await?;
        Ok(())
    }
}

#[async_trait]
impl ArchiveStore for PostgresArchiveStore {
    async fn get_replay(&self, key: &ReplayKey) -> Result<Option<StoredReplay>> {
        let row = replay_record::Entity::find()
            .filter(
                Condition::all()
                    .add(replay_record::Column::CaptureTs.eq(key.capture_ts.clone()))
                    .add(replay_record::Column::Modifier.eq(key.modifier.clone()))
                    .add(replay_record::Column::CanonicalOriginalUrl.eq(key.original_url.clone())),
            )
            .one(&self.db)
            .await?;
        let Some(row) = row else {
            return Ok(None);
        };
        if row.classification == "body_too_large" {
            return Ok(Some(StoredReplay::BodyTooLarge {
                key: key.clone(),
                observed_size: row.observed_size.unwrap_or(0).try_into().unwrap_or(0),
            }));
        }

        let blob_key = row
            .blob_key
            .ok_or_else(|| anyhow!("replay row missing blob_key for {key}"))?;
        let body = self.blobs.get_body(&blob_key).await?;
        Ok(Some(StoredReplay::Capture(ReplayRecord {
            key: key.clone(),
            status: row
                .status
                .ok_or_else(|| anyhow!("replay row missing status for {key}"))?
                .try_into()?,
            headers: serde_json::from_value(row.headers)?,
            blob_key: Some(blob_key),
            sha256: row
                .sha256
                .ok_or_else(|| anyhow!("replay row missing sha256 for {key}"))?,
            body_size: row
                .body_size
                .unwrap_or(body.len() as i64)
                .try_into()
                .unwrap_or(body.len()),
            body,
        })))
    }

    async fn put_replay(&self, replay: StoredReplay) -> Result<()> {
        match replay {
            StoredReplay::Capture(mut record) => {
                let blob = self.blobs.put_body(record.body.clone()).await?;
                record.blob_key = Some(blob.key.clone());
                record.sha256 = blob.sha256.clone();
                record.body_size = blob.size;
                let classification = if record.status >= 400 {
                    "archived_error"
                } else {
                    "served_capture"
                };
                let active = replay_record::ActiveModel {
                    capture_ts: Set(record.key.capture_ts),
                    modifier: Set(record.key.modifier),
                    canonical_original_url: Set(record.key.original_url),
                    status: Set(Some(record.status as i32)),
                    headers: Set(serde_json::to_value(&record.headers)?),
                    blob_key: Set(Some(blob.key)),
                    sha256: Set(Some(blob.sha256)),
                    body_size: Set(Some(blob.size as i64)),
                    classification: Set(classification.to_string()),
                    observed_size: Set(None),
                };
                replay_record::Entity::insert(active)
                    .on_conflict(replay_upsert_conflict())
                    .exec(&self.db)
                    .await?;
            }
            StoredReplay::BodyTooLarge { key, observed_size } => {
                let active = replay_record::ActiveModel {
                    capture_ts: Set(key.capture_ts),
                    modifier: Set(key.modifier),
                    canonical_original_url: Set(key.original_url),
                    status: Set(None),
                    headers: Set(Value::Array(Vec::new())),
                    blob_key: Set(None),
                    sha256: Set(None),
                    body_size: Set(None),
                    classification: Set("body_too_large".to_string()),
                    observed_size: Set(Some(observed_size as i64)),
                };
                replay_record::Entity::insert(active)
                    .on_conflict(replay_upsert_conflict())
                    .exec(&self.db)
                    .await?;
            }
        }
        Ok(())
    }

    async fn get_metadata(&self, key: &MetadataKey) -> Result<Option<StoredMetadata>> {
        let row = metadata_record::Entity::find()
            .filter(
                Condition::all()
                    .add(metadata_record::Column::Endpoint.eq(key.endpoint.as_str()))
                    .add(metadata_record::Column::NormalizedQuery.eq(key.normalized_query.clone())),
            )
            .one(&self.db)
            .await?;
        let Some(row) = row else {
            return Ok(None);
        };
        let endpoint = metadata_endpoint_from_str(&row.endpoint)
            .ok_or_else(|| anyhow!("metadata row has unknown endpoint {}", row.endpoint))?;
        let key = MetadataKey {
            endpoint,
            normalized_query: row.normalized_query,
        };
        if row.classification == "body_too_large" {
            return Ok(Some(StoredMetadata::BodyTooLarge {
                key,
                observed_size: row.observed_size.unwrap_or(0).try_into().unwrap_or(0),
            }));
        }

        let body = row
            .body
            .ok_or_else(|| anyhow!("metadata row missing body for {key}"))?;
        Ok(Some(StoredMetadata::Response(MetadataRecord {
            key,
            status: row
                .status
                .ok_or_else(|| anyhow!("metadata row missing status"))?
                .try_into()?,
            headers: serde_json::from_value(row.headers)?,
            sha256: row
                .sha256
                .ok_or_else(|| anyhow!("metadata row missing sha256"))?,
            body_size: row
                .body_size
                .unwrap_or(body.len() as i64)
                .try_into()
                .unwrap_or(body.len()),
            body: Bytes::from(body),
        })))
    }

    async fn put_metadata(&self, metadata: StoredMetadata) -> Result<()> {
        match metadata {
            StoredMetadata::Response(record) => {
                let active = metadata_record::ActiveModel {
                    endpoint: Set(record.key.endpoint.as_str().to_string()),
                    normalized_query: Set(record.key.normalized_query),
                    status: Set(Some(record.status as i32)),
                    headers: Set(serde_json::to_value(&record.headers)?),
                    body: Set(Some(record.body.to_vec())),
                    sha256: Set(Some(record.sha256)),
                    body_size: Set(Some(record.body_size as i64)),
                    classification: Set("served_metadata".to_string()),
                    observed_size: Set(None),
                };
                metadata_record::Entity::insert(active)
                    .on_conflict(metadata_upsert_conflict())
                    .exec(&self.db)
                    .await?;
            }
            StoredMetadata::BodyTooLarge { key, observed_size } => {
                let active = metadata_record::ActiveModel {
                    endpoint: Set(key.endpoint.as_str().to_string()),
                    normalized_query: Set(key.normalized_query),
                    status: Set(None),
                    headers: Set(Value::Array(Vec::new())),
                    body: Set(None),
                    sha256: Set(None),
                    body_size: Set(None),
                    classification: Set("body_too_large".to_string()),
                    observed_size: Set(Some(observed_size as i64)),
                };
                metadata_record::Entity::insert(active)
                    .on_conflict(metadata_upsert_conflict())
                    .exec(&self.db)
                    .await?;
            }
        }
        Ok(())
    }
}

fn replay_upsert_conflict() -> OnConflict {
    let mut conflict = OnConflict::columns([
        replay_record::Column::CaptureTs,
        replay_record::Column::Modifier,
        replay_record::Column::CanonicalOriginalUrl,
    ]);
    conflict.update_columns([
        replay_record::Column::Status,
        replay_record::Column::Headers,
        replay_record::Column::BlobKey,
        replay_record::Column::Sha256,
        replay_record::Column::BodySize,
        replay_record::Column::Classification,
        replay_record::Column::ObservedSize,
    ]);
    conflict.to_owned()
}

fn metadata_upsert_conflict() -> OnConflict {
    let mut conflict = OnConflict::columns([
        metadata_record::Column::Endpoint,
        metadata_record::Column::NormalizedQuery,
    ]);
    conflict.update_columns([
        metadata_record::Column::Status,
        metadata_record::Column::Headers,
        metadata_record::Column::Body,
        metadata_record::Column::Sha256,
        metadata_record::Column::BodySize,
        metadata_record::Column::Classification,
        metadata_record::Column::ObservedSize,
    ]);
    conflict.to_owned()
}

fn metadata_endpoint_from_str(endpoint: &str) -> Option<Endpoint> {
    match endpoint {
        "availability" => Some(Endpoint::Availability),
        "cdx" => Some(Endpoint::Cdx),
        _ => None,
    }
}

#[derive(Debug, Clone)]
pub struct ReqwestIaClient {
    web_upstream: String,
    availability_upstream: String,
    client: reqwest::Client,
    timeouts: UpstreamTimeouts,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct UpstreamTimeouts {
    pub availability: Duration,
    pub cdx: Duration,
    pub replay: Duration,
}

impl Default for UpstreamTimeouts {
    fn default() -> Self {
        Self {
            availability: DEFAULT_AVAILABILITY_TIMEOUT,
            cdx: DEFAULT_CDX_TIMEOUT,
            replay: DEFAULT_REPLAY_TIMEOUT,
        }
    }
}

impl UpstreamTimeouts {
    fn for_endpoint(self, endpoint: Endpoint) -> Duration {
        match endpoint {
            Endpoint::Availability => self.availability,
            Endpoint::Cdx => self.cdx,
            Endpoint::Replay => self.replay,
        }
    }
}

impl ReqwestIaClient {
    pub fn new(web_upstream: String, availability_upstream: String) -> Result<Self> {
        Self::with_timeouts(
            web_upstream,
            availability_upstream,
            UpstreamTimeouts::default(),
        )
    }

    pub fn with_timeouts(
        web_upstream: String,
        availability_upstream: String,
        timeouts: UpstreamTimeouts,
    ) -> Result<Self> {
        Ok(Self {
            web_upstream: web_upstream.trim_end_matches('/').to_string(),
            availability_upstream: availability_upstream.trim_end_matches('/').to_string(),
            client: reqwest::Client::builder()
                .use_rustls_tls()
                .redirect(reqwest::redirect::Policy::none())
                .user_agent("ducktape-wayback-archive/1 (+agentydragon@gmail.com)")
                .build()?,
            timeouts,
        })
    }

    async fn fetch_raw_metadata(
        &self,
        base_url: &str,
        request: &MetadataRequest,
    ) -> Result<UpstreamResponse> {
        let path = request.key.endpoint.metadata_path().ok_or_else(|| {
            anyhow!(
                "{} is not a metadata endpoint",
                request.key.endpoint.as_str()
            )
        })?;
        let query_suffix = if request.raw_query.is_empty() {
            String::new()
        } else {
            format!("?{}", request.raw_query)
        };
        let response = self
            .client
            .get(format!("{base_url}{path}{query_suffix}"))
            .header(reqwest::header::ACCEPT_ENCODING, "identity")
            .timeout(self.timeouts.for_endpoint(request.key.endpoint))
            .send()
            .await?;
        let status = response.status().as_u16();
        let headers = response.headers().clone();
        let body = response.bytes().await?;
        Ok(UpstreamResponse {
            status,
            headers,
            body,
        })
    }
}

#[async_trait]
impl IaClient for ReqwestIaClient {
    async fn fetch_replay(
        &self,
        key: &ReplayKey,
        _max_body_bytes: usize,
    ) -> Result<UpstreamResponse> {
        let response = self
            .client
            .get(format!("{}{}", self.web_upstream, key.ia_path()))
            .header(reqwest::header::ACCEPT_ENCODING, "identity")
            .timeout(self.timeouts.replay)
            .send()
            .await?;
        let status = response.status().as_u16();
        let headers = response.headers().clone();
        let body = response.bytes().await?;
        Ok(UpstreamResponse {
            status,
            headers,
            body,
        })
    }

    async fn fetch_metadata(
        &self,
        request: &MetadataRequest,
        _max_body_bytes: usize,
    ) -> Result<UpstreamResponse> {
        match request.key.endpoint {
            Endpoint::Availability => {
                self.fetch_raw_metadata(&self.availability_upstream, request)
                    .await
            }
            Endpoint::Cdx => self.fetch_raw_metadata(&self.web_upstream, request).await,
            Endpoint::Replay => Err(anyhow!("replay is not a metadata endpoint")),
        }
    }
}

#[derive(Debug, Clone)]
pub struct LimiterConfig {
    pub initial: usize,
    pub min: usize,
    pub max: usize,
    pub queue_wait: Duration,
    pub failure_cooldown: Duration,
    pub severe_failure_cooldown: Duration,
}

impl LimiterConfig {
    pub fn availability() -> Self {
        Self {
            initial: 8,
            min: 1,
            max: 32,
            queue_wait: DEFAULT_MAX_QUEUE_WAIT,
            failure_cooldown: Duration::from_secs(30),
            severe_failure_cooldown: Duration::from_secs(60),
        }
    }

    pub fn cdx() -> Self {
        Self {
            initial: 2,
            min: 1,
            max: 6,
            queue_wait: DEFAULT_MAX_QUEUE_WAIT,
            failure_cooldown: Duration::from_secs(30),
            severe_failure_cooldown: Duration::from_secs(60),
        }
    }

    pub fn replay() -> Self {
        Self {
            initial: 2,
            min: 1,
            max: 8,
            queue_wait: DEFAULT_MAX_QUEUE_WAIT,
            failure_cooldown: Duration::from_secs(30),
            severe_failure_cooldown: Duration::from_secs(60),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AcquisitionOutcome {
    Healthy,
    TransientFailure,
    RetryAfter(Duration),
}

#[derive(Debug)]
struct LimiterEvent {
    at: Instant,
    failed: bool,
}

#[derive(Debug)]
struct LimiterState {
    current_limit: usize,
    in_flight: usize,
    waiters: usize,
    backoff_until: Option<Instant>,
    events: VecDeque<LimiterEvent>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LimiterSnapshot {
    pub current_limit: usize,
    pub in_flight: usize,
    pub waiters: usize,
    pub backoff_seconds: Option<u64>,
    pub recent_events: usize,
    pub recent_failures: usize,
}

#[derive(Debug)]
pub struct AdaptiveLimiter {
    config: LimiterConfig,
    state: StdMutex<LimiterState>,
    notify: Notify,
}

#[derive(Debug)]
#[must_use = "dropping a limiter permit releases its in-flight slot"]
pub struct LimiterPermit {
    limiter: Option<Arc<AdaptiveLimiter>>,
}

#[derive(Debug)]
struct LimiterWaitGuard {
    limiter: Option<Arc<AdaptiveLimiter>>,
}

impl LimiterPermit {
    fn new(limiter: Arc<AdaptiveLimiter>) -> Self {
        Self {
            limiter: Some(limiter),
        }
    }

    pub fn record(mut self, outcome: AcquisitionOutcome) {
        if let Some(limiter) = self.limiter.take() {
            limiter.release(Some(outcome));
        }
    }
}

impl Drop for LimiterPermit {
    fn drop(&mut self) {
        if let Some(limiter) = self.limiter.take() {
            limiter.release(None);
        }
    }
}

impl LimiterWaitGuard {
    fn new(limiter: Arc<AdaptiveLimiter>) -> Self {
        {
            let mut state = limiter.lock_state();
            state.waiters += 1;
        }
        Self {
            limiter: Some(limiter),
        }
    }
}

impl Drop for LimiterWaitGuard {
    fn drop(&mut self) {
        if let Some(limiter) = self.limiter.take() {
            let mut state = limiter.lock_state();
            state.waiters = state.waiters.saturating_sub(1);
        }
    }
}

impl AdaptiveLimiter {
    pub fn new(config: LimiterConfig) -> Self {
        Self {
            state: StdMutex::new(LimiterState {
                current_limit: config.initial,
                in_flight: 0,
                waiters: 0,
                backoff_until: None,
                events: VecDeque::new(),
            }),
            config,
            notify: Notify::new(),
        }
    }

    pub async fn acquire(self: Arc<Self>) -> Option<LimiterPermit> {
        let deadline = Instant::now() + self.config.queue_wait;
        loop {
            let wait = {
                let mut state = self.lock_state();
                let now = Instant::now();
                if state.backoff_until.is_some_and(|until| until <= now) {
                    state.backoff_until = None;
                }
                if state.backoff_until.is_none() && state.in_flight < state.current_limit {
                    state.in_flight += 1;
                    return Some(LimiterPermit::new(self.clone()));
                }
                deadline.checked_duration_since(now)
            };
            let wait = wait?;
            let waiter = LimiterWaitGuard::new(self.clone());
            if timeout(wait, self.notify.notified()).await.is_err() {
                return None;
            }
            drop(waiter);
        }
    }

    pub async fn retry_after(&self) -> Option<Duration> {
        let mut state = self.lock_state();
        let now = Instant::now();
        if state.backoff_until.is_some_and(|until| until <= now) {
            state.backoff_until = None;
        }
        state
            .backoff_until
            .and_then(|until| until.checked_duration_since(now))
    }

    fn release(&self, outcome: Option<AcquisitionOutcome>) {
        let mut state = self.lock_state();
        state.in_flight = state.in_flight.saturating_sub(1);
        if let Some(outcome) = outcome {
            self.record_outcome(&mut state, outcome);
        }
        self.notify.notify_waiters();
    }

    fn record_outcome(&self, state: &mut LimiterState, outcome: AcquisitionOutcome) {
        let now = Instant::now();
        match outcome {
            AcquisitionOutcome::Healthy => self.record_health(state, now, false),
            AcquisitionOutcome::TransientFailure => {
                self.record_health(state, now, true);
                state.current_limit = (state.current_limit / 2).max(self.config.min);
                state.backoff_until = Some(now + self.config.failure_cooldown);
            }
            AcquisitionOutcome::RetryAfter(duration) => {
                self.record_health(state, now, true);
                state.current_limit = self.config.min;
                state.backoff_until = Some(now + duration);
            }
        }
    }

    fn record_health(&self, state: &mut LimiterState, now: Instant, failed: bool) {
        state.events.push_back(LimiterEvent { at: now, failed });
        while state
            .events
            .front()
            .is_some_and(|event| now.duration_since(event.at) > HEALTH_WINDOW)
        {
            state.events.pop_front();
        }
        if state.events.len() < HEALTH_MIN_SAMPLES {
            return;
        }
        let failures = state.events.iter().filter(|event| event.failed).count();
        let failure_rate = failures as f64 / state.events.len() as f64;
        if failure_rate < 0.05 && state.current_limit < self.config.max {
            state.current_limit += 1;
            state.events.clear();
        } else if failure_rate >= 0.5 {
            state.current_limit = self.config.min;
            state.backoff_until = Some(now + self.config.severe_failure_cooldown);
            state.events.clear();
        } else if failure_rate >= 0.2 {
            state.current_limit = (state.current_limit / 2).max(self.config.min);
            state.backoff_until = Some(now + self.config.failure_cooldown);
            state.events.clear();
        }
    }

    pub async fn current_limit(&self) -> usize {
        self.lock_state().current_limit
    }

    pub async fn snapshot(&self) -> LimiterSnapshot {
        let mut state = self.lock_state();
        let now = Instant::now();
        if state.backoff_until.is_some_and(|until| until <= now) {
            state.backoff_until = None;
        }
        while state
            .events
            .front()
            .is_some_and(|event| now.duration_since(event.at) > HEALTH_WINDOW)
        {
            state.events.pop_front();
        }
        LimiterSnapshot {
            current_limit: state.current_limit,
            in_flight: state.in_flight,
            waiters: state.waiters,
            backoff_seconds: state
                .backoff_until
                .and_then(|until| until.checked_duration_since(now))
                .and_then(|duration| retry_after_seconds(Some(duration))),
            recent_events: state.events.len(),
            recent_failures: state.events.iter().filter(|event| event.failed).count(),
        }
    }

    fn lock_state(&self) -> StdMutexGuard<'_, LimiterState> {
        self.state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

#[derive(Debug)]
struct FillFlights<T> {
    in_progress: HashSet<T>,
    notify: Arc<Notify>,
}

impl<T> Default for FillFlights<T> {
    fn default() -> Self {
        Self {
            in_progress: HashSet::new(),
            notify: Arc::new(Notify::new()),
        }
    }
}

pub struct ArchiveService {
    store: Arc<dyn ArchiveStore>,
    client: Arc<dyn IaClient>,
    availability_limiter: Arc<AdaptiveLimiter>,
    cdx_limiter: Arc<AdaptiveLimiter>,
    replay_limiter: Arc<AdaptiveLimiter>,
    metadata_flights: Mutex<FillFlights<MetadataKey>>,
    replay_flights: Mutex<FillFlights<ReplayKey>>,
    metrics: Arc<ArchiveMetrics>,
    max_body_bytes: usize,
    max_metadata_bytes: usize,
    queue_wait: Duration,
}

impl ArchiveService {
    pub fn new(store: Arc<dyn ArchiveStore>, client: Arc<dyn IaClient>) -> Self {
        Self {
            store,
            client,
            availability_limiter: Arc::new(AdaptiveLimiter::new(LimiterConfig::availability())),
            cdx_limiter: Arc::new(AdaptiveLimiter::new(LimiterConfig::cdx())),
            replay_limiter: Arc::new(AdaptiveLimiter::new(LimiterConfig::replay())),
            metadata_flights: Mutex::new(FillFlights::default()),
            replay_flights: Mutex::new(FillFlights::default()),
            metrics: Arc::new(ArchiveMetrics::default()),
            max_body_bytes: DEFAULT_MAX_BODY_BYTES,
            max_metadata_bytes: DEFAULT_MAX_METADATA_BYTES,
            queue_wait: DEFAULT_MAX_QUEUE_WAIT,
        }
    }

    pub fn with_endpoint_limiters(
        mut self,
        availability_limiter: Arc<AdaptiveLimiter>,
        cdx_limiter: Arc<AdaptiveLimiter>,
        replay_limiter: Arc<AdaptiveLimiter>,
        queue_wait: Duration,
    ) -> Self {
        self.availability_limiter = availability_limiter;
        self.cdx_limiter = cdx_limiter;
        self.replay_limiter = replay_limiter;
        self.queue_wait = queue_wait;
        self
    }

    pub fn with_limits(
        mut self,
        replay_limiter: Arc<AdaptiveLimiter>,
        queue_wait: Duration,
    ) -> Self {
        self.replay_limiter = replay_limiter;
        self.queue_wait = queue_wait;
        self
    }

    pub fn with_max_body_bytes(mut self, max_body_bytes: usize) -> Self {
        self.max_body_bytes = max_body_bytes;
        self
    }

    pub fn with_max_metadata_bytes(mut self, max_metadata_bytes: usize) -> Self {
        self.max_metadata_bytes = max_metadata_bytes;
        self
    }

    pub async fn handle_path(&self, path: &str) -> ArchiveResponse {
        self.handle_request(path, None).await
    }

    pub async fn handle_request(&self, path: &str, query: Option<&str>) -> ArchiveResponse {
        if let Some(request) = parse_metadata_request(path, query) {
            return self.handle_metadata(request).await;
        }
        let Some(key) = parse_replay_path(path) else {
            return ArchiveResponse::text(
                StatusCode::NOT_FOUND,
                "unsupported wayback archive path\n",
            );
        };
        self.handle_replay(key).await
    }

    pub async fn metrics(&self) -> Result<String> {
        let registry = Registry::new();
        let limit = GaugeVec::new(
            Opts::new(
                "wayback_archive_limiter_limit",
                "Current adaptive concurrency limit.",
            ),
            &["endpoint"],
        )?;
        let in_flight = GaugeVec::new(
            Opts::new(
                "wayback_archive_limiter_in_flight",
                "Current in-flight IA acquisitions.",
            ),
            &["endpoint"],
        )?;
        let limiter_queue_length = GaugeVec::new(
            Opts::new(
                "wayback_archive_limiter_queue_length",
                "Current requests waiting for endpoint limiter capacity or backoff.",
            ),
            &["archive_endpoint"],
        )?;
        let single_flight_queue_length = GaugeVec::new(
            Opts::new(
                "wayback_archive_single_flight_queue_length",
                "Current requests waiting for an identical archive fill already in flight.",
            ),
            &["archive_endpoint"],
        )?;
        let backoff_seconds = GaugeVec::new(
            Opts::new(
                "wayback_archive_limiter_backoff_seconds",
                "Remaining endpoint backoff seconds.",
            ),
            &["endpoint"],
        )?;
        let recent_events = GaugeVec::new(
            Opts::new(
                "wayback_archive_limiter_recent_events",
                "Recent limiter health samples.",
            ),
            &["endpoint"],
        )?;
        let recent_failures = GaugeVec::new(
            Opts::new(
                "wayback_archive_limiter_recent_failures",
                "Recent limiter failure samples.",
            ),
            &["endpoint"],
        )?;
        for endpoint in Endpoint::ALL {
            let limiter = match endpoint {
                Endpoint::Availability => self.availability_limiter.clone(),
                Endpoint::Cdx => self.cdx_limiter.clone(),
                Endpoint::Replay => self.replay_limiter.clone(),
            };
            let snapshot = limiter.snapshot().await;
            let labels = &[endpoint.as_str()];
            limit
                .with_label_values(labels)
                .set(snapshot.current_limit as f64);
            in_flight
                .with_label_values(labels)
                .set(snapshot.in_flight as f64);
            limiter_queue_length
                .with_label_values(labels)
                .set(snapshot.waiters as f64);
            backoff_seconds
                .with_label_values(labels)
                .set(snapshot.backoff_seconds.unwrap_or(0) as f64);
            recent_events
                .with_label_values(labels)
                .set(snapshot.recent_events as f64);
            recent_failures
                .with_label_values(labels)
                .set(snapshot.recent_failures as f64);
        }
        let single_flight_waiters = self
            .metrics
            .single_flight_waiter_counts()
            .into_iter()
            .collect::<HashMap<_, _>>();
        for endpoint in Endpoint::ALL {
            single_flight_queue_length
                .with_label_values(&[endpoint.as_str()])
                .set(*single_flight_waiters.get(&endpoint).unwrap_or(&0) as f64);
        }

        registry.register(Box::new(limit))?;
        registry.register(Box::new(in_flight))?;
        registry.register(Box::new(limiter_queue_length))?;
        registry.register(Box::new(single_flight_queue_length))?;
        registry.register(Box::new(backoff_seconds))?;
        registry.register(Box::new(recent_events))?;
        registry.register(Box::new(recent_failures))?;
        registry.register(Box::new(self.metrics.acquisition_attempts.clone()))?;
        registry.register(Box::new(self.metrics.acquisition_failures.clone()))?;
        registry.register(Box::new(self.metrics.upstream_fetch_duration.clone()))?;
        registry.register(Box::new(self.metrics.limiter_wait_duration.clone()))?;
        registry.register(Box::new(self.metrics.single_flight_wait_duration.clone()))?;

        let mut output = Vec::new();
        TextEncoder::new().encode(&registry.gather(), &mut output)?;
        String::from_utf8(output).context("prometheus text encoder emitted invalid UTF-8")
    }

    pub async fn handle_metadata(&self, request: MetadataRequest) -> ArchiveResponse {
        match self.store.get_metadata(&request.key).await {
            Ok(Some(metadata)) => return stored_metadata_response(metadata),
            Ok(None) => {}
            Err(_) => {
                return ArchiveResponse::text(
                    StatusCode::BAD_GATEWAY,
                    "archive metadata cache read failed\n",
                );
            }
        }
        if !self.enter_metadata_fill(&request.key).await {
            match self.store.get_metadata(&request.key).await {
                Ok(Some(metadata)) => return stored_metadata_response(metadata),
                Ok(None) => {}
                Err(_) => {
                    return ArchiveResponse::text(
                        StatusCode::BAD_GATEWAY,
                        "archive metadata cache read failed\n",
                    );
                }
            }
            warn!(
                "wayback archive acquisition failed endpoint={} reason={} status=none key={}",
                request.key.endpoint.as_str(),
                AcquisitionFailureReason::SingleFlightQueueTimeout.as_str(),
                request.key
            );
            self.metrics.record_acquisition_failure(
                request.key.endpoint,
                AcquisitionFailureReason::SingleFlightQueueTimeout,
                None,
            );
            return ArchiveResponse::retry_after(request.key.endpoint);
        }
        let response = self.fill_metadata(request.clone()).await;
        self.leave_metadata_fill(&request.key).await;
        response
    }

    pub async fn handle_replay(&self, key: ReplayKey) -> ArchiveResponse {
        match self.store.get_replay(&key).await {
            Ok(Some(replay)) => return stored_replay_response(replay),
            Ok(None) => {}
            Err(_) => {
                return ArchiveResponse::text(
                    StatusCode::BAD_GATEWAY,
                    "archive cache read failed\n",
                );
            }
        }
        if !self.enter_fill(&key).await {
            match self.store.get_replay(&key).await {
                Ok(Some(replay)) => return stored_replay_response(replay),
                Ok(None) => {}
                Err(_) => {
                    return ArchiveResponse::text(
                        StatusCode::BAD_GATEWAY,
                        "archive cache read failed\n",
                    );
                }
            }
            warn!(
                "wayback archive acquisition failed endpoint={} reason={} status=none key={}",
                Endpoint::Replay.as_str(),
                AcquisitionFailureReason::SingleFlightQueueTimeout.as_str(),
                key
            );
            self.metrics.record_acquisition_failure(
                Endpoint::Replay,
                AcquisitionFailureReason::SingleFlightQueueTimeout,
                None,
            );
            return ArchiveResponse::retry_after(Endpoint::Replay);
        }
        let response = self.fill_replay(key.clone()).await;
        self.leave_fill(&key).await;
        response
    }

    async fn enter_fill(&self, key: &ReplayKey) -> bool {
        let started = Instant::now();
        let deadline = Instant::now() + self.queue_wait;
        loop {
            let notify = {
                let mut flights = self.replay_flights.lock().await;
                if flights.in_progress.insert(key.clone()) {
                    self.metrics.record_single_flight_wait_duration(
                        Endpoint::Replay,
                        SingleFlightWaitOutcome::Owner,
                        started.elapsed(),
                    );
                    return true;
                }
                flights.notify.clone()
            };
            let Some(wait) = deadline.checked_duration_since(Instant::now()) else {
                self.metrics.record_single_flight_wait_duration(
                    Endpoint::Replay,
                    SingleFlightWaitOutcome::Timeout,
                    started.elapsed(),
                );
                return false;
            };
            let waiter = self.metrics.begin_single_flight_wait(Endpoint::Replay);
            if timeout(wait, notify.notified()).await.is_err() {
                drop(waiter);
                self.metrics.record_single_flight_wait_duration(
                    Endpoint::Replay,
                    SingleFlightWaitOutcome::Timeout,
                    started.elapsed(),
                );
                return false;
            }
            drop(waiter);
            if matches!(self.store.get_replay(key).await, Ok(Some(_))) {
                self.metrics.record_single_flight_wait_duration(
                    Endpoint::Replay,
                    SingleFlightWaitOutcome::FilledByPeer,
                    started.elapsed(),
                );
                return false;
            }
        }
    }

    async fn leave_fill(&self, key: &ReplayKey) {
        let notify = {
            let mut flights = self.replay_flights.lock().await;
            flights.in_progress.remove(key);
            flights.notify.clone()
        };
        notify.notify_waiters();
    }

    async fn enter_metadata_fill(&self, key: &MetadataKey) -> bool {
        let started = Instant::now();
        let deadline = Instant::now() + self.queue_wait;
        loop {
            let notify = {
                let mut flights = self.metadata_flights.lock().await;
                if flights.in_progress.insert(key.clone()) {
                    self.metrics.record_single_flight_wait_duration(
                        key.endpoint,
                        SingleFlightWaitOutcome::Owner,
                        started.elapsed(),
                    );
                    return true;
                }
                flights.notify.clone()
            };
            let Some(wait) = deadline.checked_duration_since(Instant::now()) else {
                self.metrics.record_single_flight_wait_duration(
                    key.endpoint,
                    SingleFlightWaitOutcome::Timeout,
                    started.elapsed(),
                );
                return false;
            };
            let waiter = self.metrics.begin_single_flight_wait(key.endpoint);
            if timeout(wait, notify.notified()).await.is_err() {
                drop(waiter);
                self.metrics.record_single_flight_wait_duration(
                    key.endpoint,
                    SingleFlightWaitOutcome::Timeout,
                    started.elapsed(),
                );
                return false;
            }
            drop(waiter);
            if matches!(self.store.get_metadata(key).await, Ok(Some(_))) {
                self.metrics.record_single_flight_wait_duration(
                    key.endpoint,
                    SingleFlightWaitOutcome::FilledByPeer,
                    started.elapsed(),
                );
                return false;
            }
        }
    }

    async fn leave_metadata_fill(&self, key: &MetadataKey) {
        let notify = {
            let mut flights = self.metadata_flights.lock().await;
            flights.in_progress.remove(key);
            flights.notify.clone()
        };
        notify.notify_waiters();
    }

    async fn fill_metadata(&self, request: MetadataRequest) -> ArchiveResponse {
        let limiter = self.metadata_limiter(request.key.endpoint);
        let limiter_wait_started = Instant::now();
        let Some(permit) = limiter.clone().acquire().await else {
            self.metrics.record_limiter_wait_duration(
                request.key.endpoint,
                LimiterWaitOutcome::Timeout,
                limiter_wait_started.elapsed(),
            );
            warn!(
                "wayback archive acquisition failed endpoint={} reason={} status=none key={}",
                request.key.endpoint.as_str(),
                AcquisitionFailureReason::LimiterQueueTimeout.as_str(),
                request.key
            );
            self.metrics.record_acquisition_failure(
                request.key.endpoint,
                AcquisitionFailureReason::LimiterQueueTimeout,
                None,
            );
            return ArchiveResponse::retry_after_duration(
                request.key.endpoint,
                limiter.retry_after().await,
            );
        };
        self.metrics.record_limiter_wait_duration(
            request.key.endpoint,
            LimiterWaitOutcome::Acquired,
            limiter_wait_started.elapsed(),
        );
        self.metrics
            .record_acquisition_attempt(request.key.endpoint);
        let upstream_started = Instant::now();
        let upstream = self
            .client
            .fetch_metadata(&request, self.max_metadata_bytes)
            .await;
        let upstream_duration = upstream_started.elapsed();
        match upstream {
            Ok(response) if response.body.len() > self.max_metadata_bytes => {
                self.metrics.record_upstream_fetch_duration(
                    request.key.endpoint,
                    UpstreamFetchResult::BodyTooLarge,
                    Some(response.status),
                    upstream_duration,
                );
                permit.record(AcquisitionOutcome::Healthy);
                let metadata = StoredMetadata::BodyTooLarge {
                    key: request.key,
                    observed_size: response.body.len(),
                };
                if self.store.put_metadata(metadata.clone()).await.is_err() {
                    return ArchiveResponse::text(
                        StatusCode::BAD_GATEWAY,
                        "archive metadata cache write failed\n",
                    );
                }
                stored_metadata_response(metadata)
            }
            Ok(response) if is_transient_metadata_failure(&response) => {
                let retry_after = retry_after_duration(&response.headers);
                let reason = if retry_after.is_some() {
                    AcquisitionFailureReason::UpstreamRetryAfter
                } else {
                    AcquisitionFailureReason::UpstreamTransientStatus
                };
                self.metrics.record_upstream_fetch_duration(
                    request.key.endpoint,
                    UpstreamFetchResult::from_upstream_failure(reason),
                    Some(response.status),
                    upstream_duration,
                );
                warn!(
                    "wayback archive acquisition failed endpoint={} reason={} status={} key={} retry_after={:?}",
                    request.key.endpoint.as_str(),
                    reason.as_str(),
                    response.status,
                    request.key,
                    retry_after
                );
                self.metrics.record_acquisition_failure(
                    request.key.endpoint,
                    reason,
                    Some(response.status),
                );
                permit.record(
                    retry_after
                        .map(AcquisitionOutcome::RetryAfter)
                        .unwrap_or(AcquisitionOutcome::TransientFailure),
                );
                ArchiveResponse::retry_after_duration(request.key.endpoint, retry_after)
            }
            Ok(response) => {
                self.metrics.record_upstream_fetch_duration(
                    request.key.endpoint,
                    UpstreamFetchResult::Stored,
                    Some(response.status),
                    upstream_duration,
                );
                permit.record(AcquisitionOutcome::Healthy);
                let record = MetadataRecord {
                    key: request.key,
                    status: response.status,
                    headers: selected_metadata_headers(&response.headers),
                    sha256: sha256_hex(&response.body),
                    body_size: response.body.len(),
                    body: response.body,
                };
                let metadata = StoredMetadata::Response(record);
                if self.store.put_metadata(metadata.clone()).await.is_err() {
                    return ArchiveResponse::text(
                        StatusCode::BAD_GATEWAY,
                        "archive metadata cache write failed\n",
                    );
                }
                stored_metadata_response(metadata)
            }
            Err(error) => {
                let reason = upstream_error_reason(&error);
                self.metrics.record_upstream_fetch_duration(
                    request.key.endpoint,
                    UpstreamFetchResult::from_upstream_failure(reason),
                    None,
                    upstream_duration,
                );
                warn!(
                    "wayback archive acquisition failed endpoint={} reason={} status=none key={} error={error:#}",
                    request.key.endpoint.as_str(),
                    reason.as_str(),
                    request.key
                );
                self.metrics
                    .record_acquisition_failure(request.key.endpoint, reason, None);
                permit.record(AcquisitionOutcome::TransientFailure);
                ArchiveResponse::retry_after(request.key.endpoint)
            }
        }
    }

    async fn fill_replay(&self, key: ReplayKey) -> ArchiveResponse {
        let limiter = self.replay_limiter.clone();
        let limiter_wait_started = Instant::now();
        let Some(permit) = limiter.clone().acquire().await else {
            self.metrics.record_limiter_wait_duration(
                Endpoint::Replay,
                LimiterWaitOutcome::Timeout,
                limiter_wait_started.elapsed(),
            );
            warn!(
                "wayback archive acquisition failed endpoint={} reason={} status=none key={}",
                Endpoint::Replay.as_str(),
                AcquisitionFailureReason::LimiterQueueTimeout.as_str(),
                key
            );
            self.metrics.record_acquisition_failure(
                Endpoint::Replay,
                AcquisitionFailureReason::LimiterQueueTimeout,
                None,
            );
            return ArchiveResponse::retry_after_duration(
                Endpoint::Replay,
                limiter.retry_after().await,
            );
        };
        self.metrics.record_limiter_wait_duration(
            Endpoint::Replay,
            LimiterWaitOutcome::Acquired,
            limiter_wait_started.elapsed(),
        );
        self.metrics.record_acquisition_attempt(Endpoint::Replay);
        let upstream_started = Instant::now();
        let upstream = self.client.fetch_replay(&key, self.max_body_bytes).await;
        let upstream_duration = upstream_started.elapsed();
        match upstream {
            Ok(response) if response.body.len() > self.max_body_bytes => {
                self.metrics.record_upstream_fetch_duration(
                    Endpoint::Replay,
                    UpstreamFetchResult::BodyTooLarge,
                    Some(response.status),
                    upstream_duration,
                );
                permit.record(AcquisitionOutcome::Healthy);
                let replay = StoredReplay::BodyTooLarge {
                    key,
                    observed_size: response.body.len(),
                };
                if self.store.put_replay(replay.clone()).await.is_err() {
                    return ArchiveResponse::text(
                        StatusCode::BAD_GATEWAY,
                        "archive cache write failed\n",
                    );
                }
                stored_replay_response(replay)
            }
            Ok(response) if is_transient_replay_failure(&response) => {
                let retry_after = retry_after_duration(&response.headers);
                let reason = if retry_after.is_some() {
                    AcquisitionFailureReason::UpstreamRetryAfter
                } else {
                    AcquisitionFailureReason::UpstreamTransientStatus
                };
                self.metrics.record_upstream_fetch_duration(
                    Endpoint::Replay,
                    UpstreamFetchResult::from_upstream_failure(reason),
                    Some(response.status),
                    upstream_duration,
                );
                warn!(
                    "wayback archive acquisition failed endpoint={} reason={} status={} key={} retry_after={:?}",
                    Endpoint::Replay.as_str(),
                    reason.as_str(),
                    response.status,
                    key,
                    retry_after
                );
                self.metrics.record_acquisition_failure(
                    Endpoint::Replay,
                    reason,
                    Some(response.status),
                );
                permit.record(
                    retry_after
                        .map(AcquisitionOutcome::RetryAfter)
                        .unwrap_or(AcquisitionOutcome::TransientFailure),
                );
                ArchiveResponse::retry_after_duration(Endpoint::Replay, retry_after)
            }
            Ok(response) => {
                self.metrics.record_upstream_fetch_duration(
                    Endpoint::Replay,
                    UpstreamFetchResult::Stored,
                    Some(response.status),
                    upstream_duration,
                );
                permit.record(AcquisitionOutcome::Healthy);
                let record = ReplayRecord {
                    key,
                    status: response.status,
                    headers: selected_headers(&response.headers),
                    blob_key: None,
                    sha256: sha256_hex(&response.body),
                    body_size: response.body.len(),
                    body: response.body,
                };
                let replay = StoredReplay::Capture(record);
                if self.store.put_replay(replay.clone()).await.is_err() {
                    return ArchiveResponse::text(
                        StatusCode::BAD_GATEWAY,
                        "archive cache write failed\n",
                    );
                }
                stored_replay_response(replay)
            }
            Err(error) => {
                let reason = upstream_error_reason(&error);
                self.metrics.record_upstream_fetch_duration(
                    Endpoint::Replay,
                    UpstreamFetchResult::from_upstream_failure(reason),
                    None,
                    upstream_duration,
                );
                warn!(
                    "wayback archive acquisition failed endpoint={} reason={} status=none key={} error={error:#}",
                    Endpoint::Replay.as_str(),
                    reason.as_str(),
                    key
                );
                self.metrics
                    .record_acquisition_failure(Endpoint::Replay, reason, None);
                permit.record(AcquisitionOutcome::TransientFailure);
                ArchiveResponse::retry_after(Endpoint::Replay)
            }
        }
    }

    fn metadata_limiter(&self, endpoint: Endpoint) -> Arc<AdaptiveLimiter> {
        match endpoint {
            Endpoint::Availability => self.availability_limiter.clone(),
            Endpoint::Cdx => self.cdx_limiter.clone(),
            Endpoint::Replay => self.replay_limiter.clone(),
        }
    }
}

fn stored_replay_response(replay: StoredReplay) -> ArchiveResponse {
    match replay {
        StoredReplay::Capture(record) => ArchiveResponse {
            status: record.status,
            headers: record.headers,
            body: record.body,
        },
        StoredReplay::BodyTooLarge { observed_size, .. } => ArchiveResponse::text(
            StatusCode::PAYLOAD_TOO_LARGE,
            format!("archived replay body is too large: {observed_size} bytes\n"),
        ),
    }
}

fn stored_metadata_response(metadata: StoredMetadata) -> ArchiveResponse {
    match metadata {
        StoredMetadata::Response(record) => ArchiveResponse {
            status: record.status,
            headers: record.headers,
            body: record.body,
        },
        StoredMetadata::BodyTooLarge { observed_size, .. } => ArchiveResponse::text(
            StatusCode::PAYLOAD_TOO_LARGE,
            format!("archived metadata response is too large: {observed_size} bytes\n"),
        ),
    }
}

fn selected_headers(headers: &HeaderMap) -> Vec<(String, String)> {
    ["content-type", "location", "memento-datetime"]
        .into_iter()
        .filter_map(|name| {
            headers
                .get(name)
                .and_then(|value| value.to_str().ok())
                .map(|value| (name.to_string(), value.to_string()))
        })
        .collect()
}

fn selected_metadata_headers(headers: &HeaderMap) -> Vec<(String, String)> {
    ["content-type", "retry-after"]
        .into_iter()
        .filter_map(|name| {
            headers
                .get(name)
                .and_then(|value| value.to_str().ok())
                .map(|value| (name.to_string(), value.to_string()))
        })
        .collect()
}

fn is_transient_replay_failure(response: &UpstreamResponse) -> bool {
    response.status >= 400 && response.headers.get("memento-datetime").is_none()
}

fn is_transient_metadata_failure(response: &UpstreamResponse) -> bool {
    response.status == 429 || response.status >= 500
}

fn upstream_error_reason(error: &anyhow::Error) -> AcquisitionFailureReason {
    if error.chain().any(|cause| {
        cause
            .downcast_ref::<reqwest::Error>()
            .is_some_and(reqwest::Error::is_timeout)
    }) {
        AcquisitionFailureReason::UpstreamFetchTimeout
    } else {
        AcquisitionFailureReason::UpstreamFetchError
    }
}

fn retry_after_duration(headers: &HeaderMap) -> Option<Duration> {
    headers
        .get("retry-after")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<u64>().ok())
        .map(Duration::from_secs)
}

fn retry_after_seconds(duration: Option<Duration>) -> Option<u64> {
    duration.map(|duration| duration.as_secs() + u64::from(duration.subsec_nanos() > 0))
}

pub fn parse_metadata_request(path: &str, query: Option<&str>) -> Option<MetadataRequest> {
    let endpoint = match path {
        "/wayback/available" => Endpoint::Availability,
        "/cdx/search/cdx" => Endpoint::Cdx,
        _ => return None,
    };
    let raw_query = query.unwrap_or_default().to_string();
    Some(MetadataRequest {
        key: MetadataKey {
            endpoint,
            normalized_query: normalize_query(query.unwrap_or_default()),
        },
        raw_query,
    })
}

fn normalize_query(query: &str) -> String {
    let mut parts = query
        .split('&')
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>();
    parts.sort_unstable();
    parts.join("&")
}

pub fn parse_replay_path(path: &str) -> Option<ReplayKey> {
    let rest = path.strip_prefix("/web/")?;
    let slash = rest.find('/')?;
    let ts_modifier = &rest[..slash];
    let original_url = &rest[slash + 1..];
    if original_url.is_empty() {
        return None;
    }
    let digit_count = ts_modifier.chars().take_while(char::is_ascii_digit).count();
    if !(4..=14).contains(&digit_count) {
        return None;
    }
    let capture_ts = &ts_modifier[..digit_count];
    let modifier = &ts_modifier[digit_count..];
    if !(modifier.is_empty() || (modifier.len() == 3 && modifier.ends_with('_'))) {
        return None;
    }
    Some(ReplayKey {
        capture_ts: capture_ts.to_string(),
        modifier: modifier.to_string(),
        original_url: original_url.to_string(),
    })
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicUsize, Ordering};

    use http::HeaderValue;
    use http::header::CONTENT_TYPE;
    use tokio::sync::oneshot;

    use super::*;

    fn header_value(value: &str) -> HeaderValue {
        HeaderValue::from_str(value).expect("test header value must be valid")
    }

    fn response_header(response: &ArchiveResponse, name: &str) -> Option<String> {
        response
            .headers
            .iter()
            .find(|(header_name, _)| header_name == name)
            .map(|(_, value)| value.clone())
    }

    #[test]
    fn parses_replay_path() {
        assert_eq!(
            parse_replay_path("/web/20200115103000id_/https://example.com/"),
            Some(ReplayKey {
                capture_ts: "20200115103000".to_string(),
                modifier: "id_".to_string(),
                original_url: "https://example.com/".to_string()
            })
        );
        assert!(parse_replay_path("/cdx/search/cdx").is_none());
        assert!(parse_replay_path("/web/not-a-ts/http://example.com").is_none());
    }

    #[test]
    fn parses_metadata_request_with_normalized_query() {
        assert_eq!(
            parse_metadata_request(
                "/wayback/available",
                Some("url=https://example.com/&timestamp=20200101000000")
            ),
            Some(MetadataRequest {
                key: MetadataKey {
                    endpoint: Endpoint::Availability,
                    normalized_query: "timestamp=20200101000000&url=https://example.com/"
                        .to_string(),
                },
                raw_query: "url=https://example.com/&timestamp=20200101000000".to_string(),
            })
        );
        assert_eq!(
            parse_metadata_request("/cdx/search/cdx", Some("limit=-1&url=https://example.com/"))
                .unwrap()
                .key
                .normalized_query,
            "limit=-1&url=https://example.com/"
        );
        assert!(
            parse_metadata_request("/web/20200101000000id_/https://example.com/", None).is_none()
        );
    }

    #[tokio::test]
    async fn availability_miss_is_fetched_and_cached_by_normalized_query() {
        let client = Arc::new(CountingClient::ok_json(
            200,
            br#"{"archived_snapshots":{"closest":{"available":true}}}"#,
        ));
        let service = ArchiveService::new(Arc::new(MemoryArchiveStore::new()), client.clone());

        let first = service
            .handle_request(
                "/wayback/available",
                Some("url=https://example.com/&timestamp=20200101000000"),
            )
            .await;
        let second = service
            .handle_request(
                "/wayback/available",
                Some("timestamp=20200101000000&url=https://example.com/"),
            )
            .await;

        assert_eq!(first.status, 200);
        assert_eq!(second.status, 200);
        assert_eq!(second.body, first.body);
        assert_eq!(client.calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn cdx_miss_is_fetched_and_cached() {
        let client = Arc::new(CountingClient::ok_json(
            200,
            br#"[["timestamp","original"],["20200101000000","https://example.com/"]]"#,
        ));
        let service = ArchiveService::new(Arc::new(MemoryArchiveStore::new()), client.clone());

        let first = service
            .handle_request(
                "/cdx/search/cdx",
                Some("url=https://example.com/&output=json&to=20200101000000"),
            )
            .await;
        let second = service
            .handle_request(
                "/cdx/search/cdx",
                Some("to=20200101000000&output=json&url=https://example.com/"),
            )
            .await;

        assert_eq!(first.status, 200);
        assert_eq!(second.status, 200);
        assert_eq!(second.body, first.body);
        assert_eq!(client.calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn metadata_502_is_not_cached() {
        let client = Arc::new(CountingClient::new(vec![
            UpstreamResponse {
                status: 502,
                headers: HeaderMap::new(),
                body: Bytes::from_static(b"bad gateway"),
            },
            UpstreamResponse {
                status: 200,
                headers: json_headers(),
                body: Bytes::from_static(br#"{"ok":true}"#),
            },
        ]));
        let availability = Arc::new(AdaptiveLimiter::new(LimiterConfig {
            initial: 1,
            min: 1,
            max: 1,
            queue_wait: Duration::from_millis(50),
            failure_cooldown: Duration::from_millis(1),
            severe_failure_cooldown: Duration::from_millis(1),
        }));
        let cdx = Arc::new(AdaptiveLimiter::new(LimiterConfig {
            initial: 1,
            min: 1,
            max: 1,
            queue_wait: Duration::from_millis(50),
            failure_cooldown: Duration::from_millis(1),
            severe_failure_cooldown: Duration::from_millis(1),
        }));
        let replay = Arc::new(AdaptiveLimiter::new(LimiterConfig {
            initial: 1,
            min: 1,
            max: 1,
            queue_wait: Duration::from_millis(50),
            failure_cooldown: Duration::from_millis(1),
            severe_failure_cooldown: Duration::from_millis(1),
        }));
        let service = ArchiveService::new(Arc::new(MemoryArchiveStore::new()), client.clone())
            .with_endpoint_limiters(availability, cdx, replay, Duration::from_millis(50));

        let first = service
            .handle_request(
                "/wayback/available",
                Some("url=https://example.com/&timestamp=20200101000000"),
            )
            .await;
        tokio::time::sleep(Duration::from_millis(2)).await;
        let second = service
            .handle_request(
                "/wayback/available",
                Some("url=https://example.com/&timestamp=20200101000000"),
            )
            .await;

        assert_eq!(first.status, 503);
        assert_eq!(second.status, 200);
        assert_eq!(second.body, Bytes::from_static(br#"{"ok":true}"#));
        assert_eq!(client.calls.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn metadata_retry_after_is_propagated() {
        let mut headers = HeaderMap::new();
        headers.insert("retry-after", header_value("7"));
        let client = Arc::new(CountingClient::new(vec![UpstreamResponse {
            status: 503,
            headers,
            body: Bytes::from_static(b"try later"),
        }]));
        let availability = Arc::new(AdaptiveLimiter::new(LimiterConfig {
            initial: 1,
            min: 1,
            max: 1,
            queue_wait: Duration::from_millis(50),
            failure_cooldown: Duration::from_millis(1),
            severe_failure_cooldown: Duration::from_millis(1),
        }));
        let cdx = Arc::new(AdaptiveLimiter::new(LimiterConfig {
            initial: 1,
            min: 1,
            max: 1,
            queue_wait: Duration::from_millis(50),
            failure_cooldown: Duration::from_millis(1),
            severe_failure_cooldown: Duration::from_millis(1),
        }));
        let replay = Arc::new(AdaptiveLimiter::new(LimiterConfig {
            initial: 1,
            min: 1,
            max: 1,
            queue_wait: Duration::from_millis(50),
            failure_cooldown: Duration::from_millis(1),
            severe_failure_cooldown: Duration::from_millis(1),
        }));
        let service = ArchiveService::new(Arc::new(MemoryArchiveStore::new()), client)
            .with_endpoint_limiters(availability.clone(), cdx, replay, Duration::from_millis(50));

        let response = service
            .handle_request(
                "/wayback/available",
                Some("url=https://example.com/&timestamp=20200101000000"),
            )
            .await;

        assert_eq!(response.status, 503);
        assert_eq!(
            response_header(&response, "retry-after").as_deref(),
            Some("7")
        );
        assert!(availability.snapshot().await.backoff_seconds.is_some());
        assert!(
            service
                .metrics()
                .await
                .unwrap()
                .contains("wayback_archive_limiter_backoff_seconds{endpoint=\"availability\"}")
        );
        assert!(service.metrics().await.unwrap().contains(
            "wayback_archive_acquisition_failures_total{endpoint=\"availability\",reason=\"upstream_retry_after\",status=\"503\"} 1"
        ));
    }

    #[tokio::test]
    async fn limiter_queue_timeout_is_reported_by_reason() {
        let client = Arc::new(CountingClient::ok(200, b"won't be fetched".as_slice()));
        let limiter = Arc::new(AdaptiveLimiter::new(LimiterConfig {
            initial: 1,
            min: 1,
            max: 1,
            queue_wait: Duration::from_millis(1),
            failure_cooldown: Duration::from_millis(1),
            severe_failure_cooldown: Duration::from_millis(1),
        }));
        let held = limiter.clone().acquire().await.expect("held permit");
        let service = ArchiveService::new(Arc::new(MemoryArchiveStore::new()), client.clone())
            .with_limits(limiter, Duration::from_millis(50));

        let response = service
            .handle_path("/web/20200115103000id_/https://example.com/")
            .await;

        assert_eq!(response.status, 503);
        assert_eq!(client.calls.load(Ordering::SeqCst), 0);
        assert!(service.metrics().await.unwrap().contains(
            "wayback_archive_acquisition_failures_total{endpoint=\"replay\",reason=\"limiter_queue_timeout\",status=\"none\"} 1"
        ));
        drop(held);
    }

    #[tokio::test]
    async fn oversized_metadata_is_stable_policy_result() {
        let client = Arc::new(CountingClient::ok_json(200, b"too big"));
        let service = ArchiveService::new(Arc::new(MemoryArchiveStore::new()), client.clone())
            .with_max_metadata_bytes(3);

        let first = service
            .handle_request(
                "/cdx/search/cdx",
                Some("url=https://example.com/&output=json&to=20200101000000"),
            )
            .await;
        let second = service
            .handle_request(
                "/cdx/search/cdx",
                Some("url=https://example.com/&output=json&to=20200101000000"),
            )
            .await;

        assert_eq!(first.status, 413);
        assert_eq!(second.status, 413);
        assert_eq!(client.calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn replay_miss_is_fetched_and_cached() {
        let client = Arc::new(CountingClient::ok(200, b"hello".as_slice()));
        let service = ArchiveService::new(Arc::new(MemoryArchiveStore::new()), client.clone());
        let path = "/web/20200115103000id_/https://example.com/";

        let first = service.handle_path(path).await;
        let second = service.handle_path(path).await;

        assert_eq!(first.status, 200);
        assert_eq!(first.body, Bytes::from_static(b"hello"));
        assert_eq!(second.body, Bytes::from_static(b"hello"));
        assert_eq!(client.calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn archived_404_with_memento_is_cached() {
        let mut headers = HeaderMap::new();
        headers.insert(
            "memento-datetime",
            header_value("Wed, 15 Jan 2020 10:30:00 GMT"),
        );
        let client = Arc::new(CountingClient::new(vec![UpstreamResponse {
            status: 404,
            headers,
            body: Bytes::from_static(b"archived not found"),
        }]));
        let service = ArchiveService::new(Arc::new(MemoryArchiveStore::new()), client.clone());

        let first = service
            .handle_path("/web/20200115103000id_/http://gone.example/page")
            .await;
        let second = service
            .handle_path("/web/20200115103000id_/http://gone.example/page")
            .await;

        assert_eq!(first.status, 404);
        assert_eq!(second.status, 404);
        assert_eq!(second.body, Bytes::from_static(b"archived not found"));
        assert_eq!(client.calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn ia_502_is_not_cached() {
        let client = Arc::new(CountingClient::new(vec![
            UpstreamResponse {
                status: 502,
                headers: HeaderMap::new(),
                body: Bytes::from_static(b"bad gateway"),
            },
            UpstreamResponse {
                status: 200,
                headers: content_type_headers(),
                body: Bytes::from_static(b"eventual success"),
            },
        ]));
        let limiter = Arc::new(AdaptiveLimiter::new(LimiterConfig {
            initial: 1,
            min: 1,
            max: 1,
            queue_wait: Duration::from_millis(50),
            failure_cooldown: Duration::from_millis(1),
            severe_failure_cooldown: Duration::from_millis(1),
        }));
        let service = ArchiveService::new(Arc::new(MemoryArchiveStore::new()), client.clone())
            .with_limits(limiter, Duration::from_millis(50));
        let path = "/web/20200115103000id_/https://example.com/";

        let first = service.handle_path(path).await;
        tokio::time::sleep(Duration::from_millis(2)).await;
        let second = service.handle_path(path).await;

        assert_eq!(first.status, 503);
        assert_eq!(second.status, 200);
        assert_eq!(second.body, Bytes::from_static(b"eventual success"));
        assert_eq!(client.calls.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn replay_retry_after_is_propagated() {
        let mut headers = HeaderMap::new();
        headers.insert("retry-after", header_value("11"));
        let client = Arc::new(CountingClient::new(vec![UpstreamResponse {
            status: 503,
            headers,
            body: Bytes::from_static(b"try later"),
        }]));
        let service = ArchiveService::new(Arc::new(MemoryArchiveStore::new()), client);
        let response = service
            .handle_path("/web/20200115103000id_/https://example.com/")
            .await;

        assert_eq!(response.status, 503);
        assert_eq!(
            response_header(&response, "retry-after").as_deref(),
            Some("11")
        );
    }

    #[tokio::test]
    async fn oversized_replay_is_stable_policy_result() {
        let client = Arc::new(CountingClient::ok(200, b"too big".as_slice()));
        let service = ArchiveService::new(Arc::new(MemoryArchiveStore::new()), client.clone())
            .with_max_body_bytes(3);
        let path = "/web/20200115103000id_/https://example.com/";

        let first = service.handle_path(path).await;
        let second = service.handle_path(path).await;

        assert_eq!(first.status, 413);
        assert_eq!(second.status, 413);
        assert_eq!(client.calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn concurrent_identical_misses_single_flight() {
        let (release_send, release_recv) = oneshot::channel();
        let client = Arc::new(BlockingClient {
            calls: AtomicUsize::new(0),
            release: Mutex::new(Some(release_recv)),
        });
        let service = Arc::new(ArchiveService::new(
            Arc::new(MemoryArchiveStore::new()),
            client.clone(),
        ));
        let path = "/web/20200115103000id_/https://example.com/";

        let first = tokio::spawn({
            let service = service.clone();
            async move { service.handle_path(path).await }
        });
        let second = tokio::spawn({
            let service = service.clone();
            async move { service.handle_path(path).await }
        });
        while client.calls.load(Ordering::SeqCst) == 0 {
            tokio::task::yield_now().await;
        }
        release_send.send(()).unwrap();

        let first = first.await.unwrap();
        let second = second.await.unwrap();
        assert_eq!(first.status, 200);
        assert_eq!(second.status, 200);
        assert_eq!(client.calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn limiter_reduces_limit_on_failures() {
        let limiter = Arc::new(AdaptiveLimiter::new(LimiterConfig {
            initial: 4,
            min: 1,
            max: 8,
            queue_wait: Duration::from_millis(10),
            failure_cooldown: Duration::from_millis(1),
            severe_failure_cooldown: Duration::from_millis(1),
        }));
        let permit = limiter.clone().acquire().await.expect("limiter permit");

        permit.record(AcquisitionOutcome::TransientFailure);

        assert_eq!(limiter.current_limit().await, 2);
    }

    #[tokio::test]
    async fn limiter_permit_drop_releases_in_flight_without_health_sample() {
        let limiter = Arc::new(AdaptiveLimiter::new(LimiterConfig {
            initial: 1,
            min: 1,
            max: 1,
            queue_wait: Duration::from_millis(10),
            failure_cooldown: Duration::from_millis(1),
            severe_failure_cooldown: Duration::from_millis(1),
        }));
        let permit = limiter.clone().acquire().await.expect("first permit");
        assert_eq!(limiter.snapshot().await.in_flight, 1);

        drop(permit);

        let snapshot = limiter.snapshot().await;
        assert_eq!(snapshot.in_flight, 0);
        assert_eq!(snapshot.recent_events, 0);
        let second = limiter
            .clone()
            .acquire()
            .await
            .expect("permit released on drop");
        assert_eq!(limiter.snapshot().await.in_flight, 1);
        drop(second);
    }

    #[tokio::test]
    async fn reqwest_metadata_fetch_honors_endpoint_timeout() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (_socket, _peer) = listener.accept().await.unwrap();
            tokio::time::sleep(Duration::from_secs(5)).await;
        });
        let client = ReqwestIaClient::with_timeouts(
            format!("http://{address}"),
            format!("http://{address}"),
            UpstreamTimeouts {
                availability: Duration::from_millis(100),
                cdx: Duration::from_secs(5),
                replay: Duration::from_secs(5),
            },
        )
        .unwrap();
        let request = parse_metadata_request(
            "/wayback/available",
            Some("url=https://example.com/&timestamp=20200101000000"),
        )
        .unwrap();

        let started = Instant::now();
        let result = client
            .fetch_metadata(&request, DEFAULT_MAX_METADATA_BYTES)
            .await;

        assert!(result.is_err());
        assert!(started.elapsed() < Duration::from_secs(2));
        server.abort();
    }

    fn content_type_headers() -> HeaderMap {
        let mut headers = HeaderMap::new();
        headers.insert(CONTENT_TYPE, header_value("text/html"));
        headers
    }

    fn json_headers() -> HeaderMap {
        let mut headers = HeaderMap::new();
        headers.insert(CONTENT_TYPE, header_value("application/json"));
        headers
    }

    struct CountingClient {
        calls: AtomicUsize,
        responses: Mutex<VecDeque<UpstreamResponse>>,
    }

    impl CountingClient {
        fn ok(status: u16, body: &[u8]) -> Self {
            Self::new(vec![UpstreamResponse {
                status,
                headers: content_type_headers(),
                body: Bytes::copy_from_slice(body),
            }])
        }

        fn ok_json(status: u16, body: &[u8]) -> Self {
            Self::new(vec![UpstreamResponse {
                status,
                headers: json_headers(),
                body: Bytes::copy_from_slice(body),
            }])
        }

        fn new(responses: Vec<UpstreamResponse>) -> Self {
            Self {
                calls: AtomicUsize::new(0),
                responses: Mutex::new(responses.into()),
            }
        }
    }

    #[async_trait]
    impl IaClient for CountingClient {
        async fn fetch_replay(
            &self,
            _key: &ReplayKey,
            _max_body_bytes: usize,
        ) -> Result<UpstreamResponse> {
            self.pop_response().await
        }

        async fn fetch_metadata(
            &self,
            _request: &MetadataRequest,
            _max_body_bytes: usize,
        ) -> Result<UpstreamResponse> {
            self.pop_response().await
        }
    }

    impl CountingClient {
        async fn pop_response(&self) -> Result<UpstreamResponse> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            let mut responses = self.responses.lock().await;
            Ok(responses.pop_front().unwrap_or_else(|| UpstreamResponse {
                status: 200,
                headers: content_type_headers(),
                body: Bytes::from_static(b"default success"),
            }))
        }
    }

    struct BlockingClient {
        calls: AtomicUsize,
        release: Mutex<Option<oneshot::Receiver<()>>>,
    }

    #[async_trait]
    impl IaClient for BlockingClient {
        async fn fetch_replay(
            &self,
            _key: &ReplayKey,
            _max_body_bytes: usize,
        ) -> Result<UpstreamResponse> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            if let Some(release) = self.release.lock().await.take() {
                release.await.unwrap();
            }
            Ok(UpstreamResponse {
                status: 200,
                headers: content_type_headers(),
                body: Bytes::from_static(b"single flight"),
            })
        }

        async fn fetch_metadata(
            &self,
            _request: &MetadataRequest,
            _max_body_bytes: usize,
        ) -> Result<UpstreamResponse> {
            self.fetch_replay(
                &ReplayKey {
                    capture_ts: "20200101000000".to_string(),
                    modifier: "id_".to_string(),
                    original_url: "https://example.com/".to_string(),
                },
                DEFAULT_MAX_BODY_BYTES,
            )
            .await
        }
    }
}
