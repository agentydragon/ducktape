use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use http::StatusCode;
use log::warn;
use prometheus::{Encoder, HistogramOpts, HistogramVec, Registry, TextEncoder};

use crate::path::{parse_metadata_request, parse_replay_path};
use crate::response::{ArchiveResponse, stored_metadata_response, stored_replay_response};
use crate::store::ArchiveStore;
use crate::types::{Endpoint, FillRequest, MetadataKey, MetadataRequest, ReplayKey};
use crate::{DEFAULT_MAX_QUEUE_WAIT, RETRY_AFTER_SECONDS};

const WAIT_BUCKETS: [f64; 12] = [
    0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0,
];

pub struct ArchiveInputService {
    store: Arc<dyn ArchiveStore>,
    queue_wait: Duration,
    fill_wait_duration: HistogramVec,
}

impl ArchiveInputService {
    pub fn new(store: Arc<dyn ArchiveStore>) -> Self {
        Self {
            store,
            queue_wait: DEFAULT_MAX_QUEUE_WAIT,
            fill_wait_duration: HistogramVec::new(
                HistogramOpts::new(
                    "wayback_archive_input_fill_wait_duration_seconds",
                    "Duration spent waiting for a queued fill to become available in the cache.",
                )
                .buckets(WAIT_BUCKETS.to_vec()),
                &["archive_endpoint", "outcome"],
            )
            .expect("input fill wait metric definition is valid"),
        }
    }

    pub fn with_queue_wait(mut self, queue_wait: Duration) -> Self {
        self.queue_wait = queue_wait;
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
        registry.register(Box::new(self.fill_wait_duration.clone()))?;
        let mut output = Vec::new();
        TextEncoder::new().encode(&registry.gather(), &mut output)?;
        String::from_utf8(output).context("prometheus text encoder emitted invalid UTF-8")
    }

    async fn handle_metadata(&self, request: MetadataRequest) -> ArchiveResponse {
        match self.store.get_metadata(&request.key).await {
            Ok(Some(metadata)) => return stored_metadata_response(metadata),
            Ok(None) => {}
            Err(error) => {
                warn!(
                    "metadata cache read failed key={} error={error:#}",
                    request.key
                );
                return ArchiveResponse::text(
                    StatusCode::BAD_GATEWAY,
                    "archive metadata cache read failed\n",
                );
            }
        }
        let fill = FillRequest::Metadata(request.clone());
        if let Err(error) = self.store.enqueue_fill(fill.clone()).await {
            warn!(
                "metadata fill enqueue failed key={} error={error:#}",
                request.key
            );
            return ArchiveResponse::text(StatusCode::BAD_GATEWAY, "archive fill enqueue failed\n");
        }
        self.wait_for_metadata(fill, &request.key).await
    }

    async fn handle_replay(&self, key: ReplayKey) -> ArchiveResponse {
        match self.store.get_replay(&key).await {
            Ok(Some(replay)) => return stored_replay_response(replay),
            Ok(None) => {}
            Err(error) => {
                warn!("replay cache read failed key={key} error={error:#}");
                return ArchiveResponse::text(
                    StatusCode::BAD_GATEWAY,
                    "archive cache read failed\n",
                );
            }
        }
        let fill = FillRequest::Replay(key.clone());
        if let Err(error) = self.store.enqueue_fill(fill.clone()).await {
            warn!("replay fill enqueue failed key={key} error={error:#}");
            return ArchiveResponse::text(StatusCode::BAD_GATEWAY, "archive fill enqueue failed\n");
        }
        self.wait_for_replay(fill, &key).await
    }

    async fn wait_for_metadata(&self, fill: FillRequest, key: &MetadataKey) -> ArchiveResponse {
        let started = Instant::now();
        let deadline = started + self.queue_wait;
        let lease_key = fill.lease_key();
        loop {
            match self.store.get_metadata(key).await {
                Ok(Some(metadata)) => {
                    self.record_fill_wait(key.endpoint, "filled", started.elapsed());
                    return stored_metadata_response(metadata);
                }
                Ok(None) => {}
                Err(error) => {
                    warn!("metadata cache read failed key={key} error={error:#}");
                    return ArchiveResponse::text(
                        StatusCode::BAD_GATEWAY,
                        "archive metadata cache read failed\n",
                    );
                }
            }
            let Some(wait) = deadline.checked_duration_since(Instant::now()) else {
                self.record_fill_wait(key.endpoint, "timeout", started.elapsed());
                return ArchiveResponse::retry_after_duration(
                    key.endpoint,
                    Some(Duration::from_secs(RETRY_AFTER_SECONDS)),
                );
            };
            match self.store.wait_for_fill_change(&lease_key, wait).await {
                Ok(_) => {}
                Err(error) => {
                    warn!("fill wait failed key={lease_key} error={error:#}");
                    self.record_fill_wait(key.endpoint, "error", started.elapsed());
                    return ArchiveResponse::text(
                        StatusCode::BAD_GATEWAY,
                        "archive fill wait failed\n",
                    );
                }
            }
        }
    }

    async fn wait_for_replay(&self, fill: FillRequest, key: &ReplayKey) -> ArchiveResponse {
        let started = Instant::now();
        let deadline = started + self.queue_wait;
        let lease_key = fill.lease_key();
        loop {
            match self.store.get_replay(key).await {
                Ok(Some(replay)) => {
                    self.record_fill_wait(Endpoint::Replay, "filled", started.elapsed());
                    return stored_replay_response(replay);
                }
                Ok(None) => {}
                Err(error) => {
                    warn!("replay cache read failed key={key} error={error:#}");
                    return ArchiveResponse::text(
                        StatusCode::BAD_GATEWAY,
                        "archive cache read failed\n",
                    );
                }
            }
            let Some(wait) = deadline.checked_duration_since(Instant::now()) else {
                self.record_fill_wait(Endpoint::Replay, "timeout", started.elapsed());
                return ArchiveResponse::retry_after_duration(
                    Endpoint::Replay,
                    Some(Duration::from_secs(RETRY_AFTER_SECONDS)),
                );
            };
            match self.store.wait_for_fill_change(&lease_key, wait).await {
                Ok(_) => {}
                Err(error) => {
                    warn!("fill wait failed key={lease_key} error={error:#}");
                    self.record_fill_wait(Endpoint::Replay, "error", started.elapsed());
                    return ArchiveResponse::text(
                        StatusCode::BAD_GATEWAY,
                        "archive fill wait failed\n",
                    );
                }
            }
        }
    }

    fn record_fill_wait(&self, endpoint: Endpoint, outcome: &str, duration: Duration) {
        self.fill_wait_duration
            .with_label_values(&[endpoint.as_str(), outcome])
            .observe(duration.as_secs_f64());
    }
}
