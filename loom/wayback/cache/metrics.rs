use std::collections::HashMap;
use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;

use prometheus::{HistogramOpts, HistogramVec, IntCounterVec, Opts};

use crate::types::Endpoint;

const DURATION_BUCKETS: [f64; 16] = [
    0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 45.0, 60.0, 90.0, 120.0, 180.0, 300.0,
];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub(crate) enum AcquisitionFailureReason {
    SingleFlightQueueTimeout,
    LimiterQueueTimeout,
    UpstreamRetryAfter,
    UpstreamTransientStatus,
    UpstreamFetchTimeout,
    UpstreamFetchError,
}

impl AcquisitionFailureReason {
    pub(crate) fn as_str(self) -> &'static str {
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
pub(crate) enum UpstreamFetchResult {
    Stored,
    BodyTooLarge,
    UpstreamRetryAfter,
    UpstreamTransientStatus,
    UpstreamFetchTimeout,
    UpstreamFetchError,
}

impl UpstreamFetchResult {
    pub(crate) fn outcome(self) -> &'static str {
        match self {
            UpstreamFetchResult::Stored | UpstreamFetchResult::BodyTooLarge => "success",
            UpstreamFetchResult::UpstreamRetryAfter
            | UpstreamFetchResult::UpstreamTransientStatus
            | UpstreamFetchResult::UpstreamFetchTimeout
            | UpstreamFetchResult::UpstreamFetchError => "failure",
        }
    }

    pub(crate) fn reason(self) -> &'static str {
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
    pub(crate) fn from_upstream_failure(reason: AcquisitionFailureReason) -> Self {
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
pub(crate) enum LimiterWaitOutcome {
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
pub(crate) enum SingleFlightWaitOutcome {
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
pub(crate) struct ArchiveMetrics {
    pub(crate) acquisition_attempts: IntCounterVec,
    pub(crate) acquisition_failures: IntCounterVec,
    pub(crate) upstream_fetch_duration: HistogramVec,
    pub(crate) limiter_wait_duration: HistogramVec,
    pub(crate) single_flight_wait_duration: HistogramVec,
    single_flight_waiters: StdMutex<HashMap<Endpoint, usize>>,
}

impl Default for ArchiveMetrics {
    fn default() -> Self {
        Self {
            acquisition_attempts: IntCounterVec::new(
                Opts::new(
                    "wayback_cache_acquisition_attempts_total",
                    "Cache miss acquisitions attempted against IA.",
                ),
                &["endpoint"],
            )
            .expect("acquisition_attempts metric definition is valid"),
            acquisition_failures: IntCounterVec::new(
                Opts::new(
                    "wayback_cache_acquisition_failures_total",
                    "Cache miss acquisitions that returned a retryable/backpressure response before storing a result.",
                ),
                &["endpoint", "reason", "status"],
            )
            .expect("acquisition_failures metric definition is valid"),
            upstream_fetch_duration: HistogramVec::new(
                HistogramOpts::new(
                    "wayback_cache_upstream_fetch_duration_seconds",
                    "Duration of upstream IA fetches after acquiring an endpoint limiter slot.",
                )
                .buckets(DURATION_BUCKETS.to_vec()),
                &["cache_endpoint", "outcome", "reason", "status"],
            )
            .expect("upstream_fetch_duration metric definition is valid"),
            limiter_wait_duration: HistogramVec::new(
                HistogramOpts::new(
                    "wayback_cache_limiter_wait_duration_seconds",
                    "Duration spent waiting for endpoint limiter capacity or backoff.",
                )
                .buckets(DURATION_BUCKETS.to_vec()),
                &["cache_endpoint", "outcome"],
            )
            .expect("limiter_wait_duration metric definition is valid"),
            single_flight_wait_duration: HistogramVec::new(
                HistogramOpts::new(
                    "wayback_cache_single_flight_wait_duration_seconds",
                    "Duration spent waiting for an identical cache fill already in flight.",
                )
                .buckets(DURATION_BUCKETS.to_vec()),
                &["cache_endpoint", "outcome"],
            )
            .expect("single_flight_wait_duration metric definition is valid"),
            single_flight_waiters: StdMutex::new(HashMap::new()),
        }
    }
}

impl ArchiveMetrics {
    pub(crate) fn record_acquisition_attempt(&self, endpoint: Endpoint) {
        self.acquisition_attempts
            .with_label_values(&[endpoint.as_str()])
            .inc();
    }

    pub(crate) fn record_acquisition_failure(
        &self,
        endpoint: Endpoint,
        reason: AcquisitionFailureReason,
        status: Option<u16>,
    ) {
        self.acquisition_failures
            .with_label_values(&[endpoint.as_str(), reason.as_str(), &status_label(status)])
            .inc();
    }

    pub(crate) fn record_upstream_fetch_duration(
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

    pub(crate) fn record_limiter_wait_duration(
        &self,
        endpoint: Endpoint,
        outcome: LimiterWaitOutcome,
        duration: Duration,
    ) {
        self.limiter_wait_duration
            .with_label_values(&[endpoint.as_str(), outcome.as_str()])
            .observe(duration.as_secs_f64());
    }

    pub(crate) fn record_single_flight_wait_duration(
        &self,
        endpoint: Endpoint,
        outcome: SingleFlightWaitOutcome,
        duration: Duration,
    ) {
        self.single_flight_wait_duration
            .with_label_values(&[endpoint.as_str(), outcome.as_str()])
            .observe(duration.as_secs_f64());
    }

    pub(crate) fn begin_single_flight_wait(
        self: &Arc<Self>,
        endpoint: Endpoint,
    ) -> SingleFlightWaitGuard {
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

    pub(crate) fn single_flight_waiter_counts(&self) -> Vec<(Endpoint, usize)> {
        self.single_flight_waiters
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .iter()
            .map(|(endpoint, count)| (*endpoint, *count))
            .collect()
    }
}

#[derive(Debug)]
pub(crate) struct SingleFlightWaitGuard {
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
