use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use http::{HeaderMap, HeaderName, StatusCode};
use log::warn;
use prometheus::{Encoder, GaugeVec, Opts, Registry, TextEncoder};
use tokio::sync::{Mutex, Notify};
use tokio::time::{sleep, timeout};

use crate::ia_client::{IaClient, UpstreamResponse};
use crate::limiter::{AcquisitionOutcome, AdaptiveLimiter, LimiterConfig};
use crate::metrics::{
    AcquisitionFailureReason, ArchiveMetrics, LimiterWaitOutcome, SingleFlightWaitOutcome,
    UpstreamFetchResult,
};
use crate::path::{parse_metadata_request, parse_replay_path};
use crate::response::{ArchiveResponse, stored_metadata_response, stored_replay_response};
use crate::store::ArchiveStore;
use crate::types::{
    Endpoint, FillAttemptResult, FillLeaseKey, FillRequest, MetadataKey, MetadataRecord,
    MetadataRequest, ReplayKey, ReplayRecord, StoredMetadata, StoredReplay,
};
use crate::util::sha256_hex;
use crate::{DEFAULT_MAX_BODY_BYTES, DEFAULT_MAX_METADATA_BYTES, DEFAULT_MAX_QUEUE_WAIT};

const SHARED_FILL_POLL_INTERVAL: Duration = Duration::from_millis(250);
static NEXT_INSTANCE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

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
    instance_id: String,
    lease_counter: AtomicU64,
}

impl ArchiveService {
    pub fn new(store: Arc<dyn ArchiveStore>, client: Arc<dyn IaClient>) -> Self {
        let hostname = std::env::var("HOSTNAME").unwrap_or_else(|_| "local".to_string());
        let instance_sequence = NEXT_INSTANCE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
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
            instance_id: format!("{hostname}:{}:{instance_sequence}", std::process::id()),
            lease_counter: AtomicU64::new(0),
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
                "unsupported wayback cache path\n",
            );
        };
        self.handle_replay(key).await
    }

    pub async fn metrics(&self) -> Result<String> {
        let registry = Registry::new();
        let limit = GaugeVec::new(
            Opts::new(
                "wayback_cache_limiter_limit",
                "Current adaptive concurrency limit.",
            ),
            &["endpoint"],
        )?;
        let in_flight = GaugeVec::new(
            Opts::new(
                "wayback_cache_limiter_in_flight",
                "Current in-flight IA acquisitions.",
            ),
            &["endpoint"],
        )?;
        let limiter_queue_length = GaugeVec::new(
            Opts::new(
                "wayback_cache_limiter_queue_length",
                "Current requests waiting for endpoint limiter capacity or backoff.",
            ),
            &["cache_endpoint"],
        )?;
        let single_flight_queue_length = GaugeVec::new(
            Opts::new(
                "wayback_cache_single_flight_queue_length",
                "Current requests waiting for an identical cache fill already in flight.",
            ),
            &["cache_endpoint"],
        )?;
        let backoff_seconds = GaugeVec::new(
            Opts::new(
                "wayback_cache_limiter_backoff_seconds",
                "Remaining endpoint backoff seconds.",
            ),
            &["endpoint"],
        )?;
        let recent_events = GaugeVec::new(
            Opts::new(
                "wayback_cache_limiter_recent_events",
                "Recent limiter health samples.",
            ),
            &["endpoint"],
        )?;
        let recent_failures = GaugeVec::new(
            Opts::new(
                "wayback_cache_limiter_recent_failures",
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

    pub async fn process_fill_request(&self, request: FillRequest) -> FillAttemptResult {
        let response = match request {
            FillRequest::Metadata(request) => self.fill_metadata(request).await,
            FillRequest::Replay(key) => self.fill_replay(key).await,
        };
        fill_response_result(&response)
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
                "acquisition failed endpoint={} reason={} status=none key={}",
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
        let response = self.fill_metadata_with_shared_lease(request.clone()).await;
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
                "acquisition failed endpoint={} reason={} status=none key={}",
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
        let response = self.fill_replay_with_shared_lease(key.clone()).await;
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

    async fn fill_metadata_with_shared_lease(&self, request: MetadataRequest) -> ArchiveResponse {
        let endpoint = request.key.endpoint;
        let lease_key = FillLeaseKey::metadata(&request.key);
        let owner = self.next_lease_owner();
        let started = Instant::now();
        let deadline = started + self.queue_wait;
        let mut waiter = None;
        loop {
            match self.store.get_metadata(&request.key).await {
                Ok(Some(metadata)) => {
                    drop(waiter);
                    self.metrics.record_single_flight_wait_duration(
                        endpoint,
                        SingleFlightWaitOutcome::FilledByPeer,
                        started.elapsed(),
                    );
                    return stored_metadata_response(metadata);
                }
                Ok(None) => {}
                Err(error) => {
                    warn!(
                        "metadata cache read failed while waiting for lease key={lease_key} error={error:#}"
                    );
                    return ArchiveResponse::text(
                        StatusCode::BAD_GATEWAY,
                        "archive metadata cache read failed\n",
                    );
                }
            }
            match self
                .store
                .try_acquire_fill_lease(&lease_key, &owner, self.fill_lease_ttl())
                .await
            {
                Ok(true) => {
                    if let Some(waiter) = waiter.take() {
                        drop(waiter);
                        self.metrics.record_single_flight_wait_duration(
                            endpoint,
                            SingleFlightWaitOutcome::Owner,
                            started.elapsed(),
                        );
                    }
                    let response = self.fill_metadata(request.clone()).await;
                    if let Err(error) = self.store.release_fill_lease(&lease_key, &owner).await {
                        warn!("fill lease release failed key={lease_key} error={error:#}");
                    }
                    return response;
                }
                Ok(false) => {}
                Err(error) => {
                    warn!("fill lease acquire failed key={lease_key} error={error:#}");
                    return self.fill_metadata(request).await;
                }
            }

            if waiter.is_none() {
                waiter = Some(self.metrics.begin_single_flight_wait(endpoint));
            }
            let Some(wait) = deadline.checked_duration_since(Instant::now()) else {
                drop(waiter);
                self.metrics.record_single_flight_wait_duration(
                    endpoint,
                    SingleFlightWaitOutcome::Timeout,
                    started.elapsed(),
                );
                self.record_shared_fill_timeout(endpoint, &lease_key);
                return ArchiveResponse::retry_after(endpoint);
            };
            sleep(wait.min(SHARED_FILL_POLL_INTERVAL)).await;
        }
    }

    async fn fill_replay_with_shared_lease(&self, key: ReplayKey) -> ArchiveResponse {
        let lease_key = FillLeaseKey::replay(&key);
        let owner = self.next_lease_owner();
        let started = Instant::now();
        let deadline = started + self.queue_wait;
        let mut waiter = None;
        loop {
            match self.store.get_replay(&key).await {
                Ok(Some(replay)) => {
                    drop(waiter);
                    self.metrics.record_single_flight_wait_duration(
                        Endpoint::Replay,
                        SingleFlightWaitOutcome::FilledByPeer,
                        started.elapsed(),
                    );
                    return stored_replay_response(replay);
                }
                Ok(None) => {}
                Err(error) => {
                    warn!(
                        "cache read failed while waiting for lease key={lease_key} error={error:#}"
                    );
                    return ArchiveResponse::text(
                        StatusCode::BAD_GATEWAY,
                        "archive cache read failed\n",
                    );
                }
            }
            match self
                .store
                .try_acquire_fill_lease(&lease_key, &owner, self.fill_lease_ttl())
                .await
            {
                Ok(true) => {
                    if let Some(waiter) = waiter.take() {
                        drop(waiter);
                        self.metrics.record_single_flight_wait_duration(
                            Endpoint::Replay,
                            SingleFlightWaitOutcome::Owner,
                            started.elapsed(),
                        );
                    }
                    let response = self.fill_replay(key.clone()).await;
                    if let Err(error) = self.store.release_fill_lease(&lease_key, &owner).await {
                        warn!("fill lease release failed key={lease_key} error={error:#}");
                    }
                    return response;
                }
                Ok(false) => {}
                Err(error) => {
                    warn!("fill lease acquire failed key={lease_key} error={error:#}");
                    return self.fill_replay(key).await;
                }
            }

            if waiter.is_none() {
                waiter = Some(self.metrics.begin_single_flight_wait(Endpoint::Replay));
            }
            let Some(wait) = deadline.checked_duration_since(Instant::now()) else {
                drop(waiter);
                self.metrics.record_single_flight_wait_duration(
                    Endpoint::Replay,
                    SingleFlightWaitOutcome::Timeout,
                    started.elapsed(),
                );
                self.record_shared_fill_timeout(Endpoint::Replay, &lease_key);
                return ArchiveResponse::retry_after(Endpoint::Replay);
            };
            sleep(wait.min(SHARED_FILL_POLL_INTERVAL)).await;
        }
    }

    fn next_lease_owner(&self) -> String {
        let sequence = self.lease_counter.fetch_add(1, Ordering::Relaxed);
        format!("{}:{sequence}", self.instance_id)
    }

    fn fill_lease_ttl(&self) -> Duration {
        self.queue_wait + Duration::from_secs(120)
    }

    fn record_shared_fill_timeout(&self, endpoint: Endpoint, key: &FillLeaseKey) {
        warn!(
            "acquisition failed endpoint={} reason={} status=none key={}",
            endpoint.as_str(),
            AcquisitionFailureReason::SingleFlightQueueTimeout.as_str(),
            key
        );
        self.metrics.record_acquisition_failure(
            endpoint,
            AcquisitionFailureReason::SingleFlightQueueTimeout,
            None,
        );
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
                "acquisition failed endpoint={} reason={} status=none key={}",
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
                    Some(response.status.as_u16()),
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
                    Some(response.status.as_u16()),
                    upstream_duration,
                );
                warn!(
                    "acquisition failed endpoint={} reason={} status={} key={} retry_after={:?}",
                    request.key.endpoint.as_str(),
                    reason.as_str(),
                    response.status.as_u16(),
                    request.key,
                    retry_after
                );
                self.metrics.record_acquisition_failure(
                    request.key.endpoint,
                    reason,
                    Some(response.status.as_u16()),
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
                    Some(response.status.as_u16()),
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
                    "acquisition failed endpoint={} reason={} status=none key={} error={error:#}",
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
                "acquisition failed endpoint={} reason={} status=none key={}",
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
                    Some(response.status.as_u16()),
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
                    Some(response.status.as_u16()),
                    upstream_duration,
                );
                warn!(
                    "acquisition failed endpoint={} reason={} status={} key={} retry_after={:?}",
                    Endpoint::Replay.as_str(),
                    reason.as_str(),
                    response.status.as_u16(),
                    key,
                    retry_after
                );
                self.metrics.record_acquisition_failure(
                    Endpoint::Replay,
                    reason,
                    Some(response.status.as_u16()),
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
                    Some(response.status.as_u16()),
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
                    "acquisition failed endpoint={} reason={} status=none key={} error={error:#}",
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

fn fill_response_result(response: &ArchiveResponse) -> FillAttemptResult {
    if response.status == StatusCode::SERVICE_UNAVAILABLE
        || response.status == StatusCode::BAD_GATEWAY
        || response.status == StatusCode::GATEWAY_TIMEOUT
    {
        return FillAttemptResult::RetryAfter {
            retry_after: retry_after_response_duration(response),
            status: Some(response.status.as_u16()),
        };
    }
    FillAttemptResult::Completed
}

fn retry_after_response_duration(response: &ArchiveResponse) -> Option<Duration> {
    response
        .headers
        .get("retry-after")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<u64>().ok())
        .map(Duration::from_secs)
}

fn selected_headers(headers: &HeaderMap) -> HeaderMap {
    selected_header_names(headers, &["content-type", "location", "memento-datetime"])
}

fn selected_metadata_headers(headers: &HeaderMap) -> HeaderMap {
    selected_header_names(headers, &["content-type", "retry-after"])
}

fn selected_header_names(headers: &HeaderMap, names: &[&'static str]) -> HeaderMap {
    let mut selected = HeaderMap::new();
    for name in names {
        if let Some(value) = headers.get(*name) {
            selected.insert(HeaderName::from_static(name), value.clone());
        }
    }
    selected
}

fn is_transient_replay_failure(response: &UpstreamResponse) -> bool {
    (response.status.is_client_error() || response.status.is_server_error())
        && response.headers.get("memento-datetime").is_none()
}

fn is_transient_metadata_failure(response: &UpstreamResponse) -> bool {
    response.status == StatusCode::TOO_MANY_REQUESTS || response.status.is_server_error()
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
