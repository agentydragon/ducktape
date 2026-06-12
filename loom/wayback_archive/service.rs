use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use http::{HeaderMap, StatusCode};
use log::warn;
use prometheus::{Encoder, GaugeVec, Opts, Registry, TextEncoder};
use tokio::sync::{Mutex, Notify};
use tokio::time::timeout;

use crate::ia_client::{IaClient, UpstreamResponse};
use crate::limiter::{AcquisitionOutcome, AdaptiveLimiter, LimiterConfig};
use crate::metrics::{
    AcquisitionFailureReason, ArchiveMetrics, LimiterWaitOutcome, SingleFlightWaitOutcome,
    UpstreamFetchResult,
};
use crate::path::{parse_metadata_request, parse_replay_path};
use crate::response::ArchiveResponse;
use crate::store::ArchiveStore;
use crate::types::{
    Endpoint, MetadataKey, MetadataRecord, MetadataRequest, ReplayKey, ReplayRecord,
    StoredMetadata, StoredReplay,
};
use crate::util::sha256_hex;
use crate::{DEFAULT_MAX_BODY_BYTES, DEFAULT_MAX_METADATA_BYTES, DEFAULT_MAX_QUEUE_WAIT};

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
