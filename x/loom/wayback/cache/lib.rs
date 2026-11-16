mod backend;
mod frontend;
mod ia_client;
mod limiter;
mod metrics;
mod path;
mod response;
mod service;
mod shared;
mod store;
mod types;
mod util;

pub use backend::ArchiveFiller;
pub use frontend::ArchiveInputService;
pub use ia_client::{IaClient, ReqwestIaClient, UpstreamResponse, UpstreamTimeouts};
pub use limiter::{
    AcquisitionOutcome, AdaptiveLimiter, LimiterConfig, LimiterPermit, LimiterSnapshot,
};
pub use path::{parse_metadata_request, parse_replay_path};
pub use response::ArchiveResponse;
pub use service::ArchiveService;
pub use shared::{
    ArchiveConfig, ArchiveSettings, archive_config_from_env, archive_store_from_config,
};
pub use store::{
    ArchiveStore, BlobRef, BlobStore, MemoryArchiveStore, MemoryBlobStore, PostgresArchiveStore,
    S3BlobStore,
};
pub use types::{
    Endpoint, FillAttemptResult, FillLeaseKey, FillRequest, MetadataKey, MetadataRecord,
    MetadataRequest, ReplayKey, ReplayRecord, StoredMetadata, StoredReplay,
};

use std::time::Duration;

pub const DEFAULT_MAX_QUEUE_WAIT: Duration = Duration::from_secs(60);
pub const DEFAULT_MAX_BODY_BYTES: usize = 10 * 1024 * 1024;
pub const DEFAULT_MAX_METADATA_BYTES: usize = 10 * 1024 * 1024;
pub const DEFAULT_AVAILABILITY_TIMEOUT: Duration = Duration::from_secs(15);
pub const DEFAULT_CDX_TIMEOUT: Duration = Duration::from_secs(45);
pub const DEFAULT_REPLAY_TIMEOUT: Duration = Duration::from_secs(60);

pub(crate) const RETRY_AFTER_SECONDS: u64 = 30;

#[cfg(test)]
mod tests {
    use std::collections::VecDeque;
    use std::sync::Arc;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::time::Duration;

    use anyhow::Result;
    use async_trait::async_trait;
    use bytes::Bytes;
    use http::header::CONTENT_TYPE;
    use http::{HeaderMap, HeaderValue, StatusCode};
    use tokio::sync::Mutex;

    use super::*;

    fn header_value(value: &str) -> HeaderValue {
        HeaderValue::from_str(value).expect("test header value must be valid")
    }

    fn response_header(response: &ArchiveResponse, name: &str) -> Option<String> {
        response
            .headers
            .get(name)
            .and_then(|value| value.to_str().ok())
            .map(str::to_string)
    }

    fn content_type_headers(value: &'static str) -> HeaderMap {
        let mut headers = HeaderMap::new();
        headers.insert(CONTENT_TYPE, HeaderValue::from_static(value));
        headers
    }

    fn upstream_response(
        status: StatusCode,
        headers: HeaderMap,
        body: &'static [u8],
    ) -> UpstreamResponse {
        UpstreamResponse {
            status,
            headers,
            body: Bytes::from_static(body),
        }
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
    async fn availability_miss_is_filled_and_cached_by_normalized_query() {
        let store = Arc::new(MemoryArchiveStore::new());
        let client = Arc::new(CountingClient::new(vec![upstream_response(
            StatusCode::OK,
            content_type_headers("application/json"),
            br#"{"archived_snapshots":{"closest":{"available":true}}}"#,
        )]));
        let worker_service = Arc::new(ArchiveService::new(store.clone(), client.clone()));
        let (input, worker) = input_worker(&store, worker_service, Duration::from_millis(100));

        let first = serve_miss(
            input.clone(),
            worker,
            "/wayback/available",
            Some("url=https://example.com/&timestamp=20200101000000"),
        )
        .await;
        let second = input
            .handle_request(
                "/wayback/available",
                Some("timestamp=20200101000000&url=https://example.com/"),
            )
            .await;

        assert_eq!(first.status, StatusCode::OK);
        assert_eq!(second.status, StatusCode::OK);
        assert_eq!(second.body, first.body);
        assert_eq!(client.calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn replay_miss_is_filled_and_cached() {
        let store = Arc::new(MemoryArchiveStore::new());
        let client = Arc::new(CountingClient::new(vec![upstream_response(
            StatusCode::OK,
            content_type_headers("text/html"),
            b"hello",
        )]));
        let worker_service = Arc::new(ArchiveService::new(store.clone(), client.clone()));
        let (input, worker) = input_worker(&store, worker_service, Duration::from_millis(100));
        let path = "/web/20200115103000id_/https://example.com/";

        let first = serve_miss(input.clone(), worker, path, None).await;
        let second = input.handle_request(path, None).await;

        assert_eq!(first.status, StatusCode::OK);
        assert_eq!(first.body, Bytes::from_static(b"hello"));
        assert_eq!(second.body, first.body);
        assert_eq!(client.calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn archived_404_with_memento_is_filled_and_cached() {
        let store = Arc::new(MemoryArchiveStore::new());
        let mut headers = content_type_headers("text/html");
        headers.insert(
            "memento-datetime",
            header_value("Wed, 15 Jan 2020 10:30:00 GMT"),
        );
        let client = Arc::new(CountingClient::new(vec![upstream_response(
            StatusCode::NOT_FOUND,
            headers,
            b"archived not found",
        )]));
        let worker_service = Arc::new(ArchiveService::new(store.clone(), client.clone()));
        let (input, worker) = input_worker(&store, worker_service, Duration::from_millis(100));
        let path = "/web/20200115103000id_/http://gone.example/page";

        let first = serve_miss(input.clone(), worker, path, None).await;
        let second = input.handle_request(path, None).await;

        assert_eq!(first.status, StatusCode::NOT_FOUND);
        assert_eq!(second.status, StatusCode::NOT_FOUND);
        assert_eq!(second.body, Bytes::from_static(b"archived not found"));
        assert_eq!(client.calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn oversized_metadata_is_a_stable_policy_result() {
        let store = Arc::new(MemoryArchiveStore::new());
        let client = Arc::new(CountingClient::new(vec![upstream_response(
            StatusCode::OK,
            content_type_headers("application/json"),
            b"too big",
        )]));
        let worker_service =
            Arc::new(ArchiveService::new(store.clone(), client.clone()).with_max_metadata_bytes(3));
        let (input, worker) = input_worker(&store, worker_service, Duration::from_millis(100));
        let path = "/cdx/search/cdx";
        let query = Some("url=https://example.com/&output=json&to=20200101000000");

        let first = serve_miss(input.clone(), worker, path, query).await;
        let second = input.handle_request(path, query).await;

        assert_eq!(first.status, StatusCode::PAYLOAD_TOO_LARGE);
        assert_eq!(second.status, StatusCode::PAYLOAD_TOO_LARGE);
        assert_eq!(client.calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn oversized_replay_is_a_stable_policy_result() {
        let store = Arc::new(MemoryArchiveStore::new());
        let client = Arc::new(CountingClient::new(vec![upstream_response(
            StatusCode::OK,
            content_type_headers("text/html"),
            b"too big",
        )]));
        let worker_service =
            Arc::new(ArchiveService::new(store.clone(), client.clone()).with_max_body_bytes(3));
        let (input, worker) = input_worker(&store, worker_service, Duration::from_millis(100));
        let path = "/web/20200115103000id_/https://example.com/";

        let first = serve_miss(input.clone(), worker, path, None).await;
        let second = input.handle_request(path, None).await;

        assert_eq!(first.status, StatusCode::PAYLOAD_TOO_LARGE);
        assert_eq!(second.status, StatusCode::PAYLOAD_TOO_LARGE);
        assert_eq!(client.calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn input_timeout_coalesces_duplicate_misses_into_one_worker_fill() {
        let store = Arc::new(MemoryArchiveStore::new());
        let client = Arc::new(CountingClient::new(vec![upstream_response(
            StatusCode::OK,
            content_type_headers("text/html"),
            b"single queued fill",
        )]));
        let worker_service = Arc::new(ArchiveService::new(store.clone(), client.clone()));
        let input = Arc::new(
            ArchiveInputService::new(store.clone()).with_queue_wait(Duration::from_millis(10)),
        );
        let worker = Arc::new(ArchiveFiller::new(store, worker_service));
        let path = "/web/20200115103000id_/https://example.com/";

        let first = input.handle_request(path, None).await;
        let second = input.handle_request(path, None).await;

        assert_eq!(first.status, StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(second.status, StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(
            response_header(&first, "retry-after").as_deref(),
            Some("30")
        );
        assert_eq!(
            response_header(&second, "retry-after").as_deref(),
            Some("30")
        );
        assert_eq!(client.calls.load(Ordering::SeqCst), 0);

        assert!(worker.run_once().await.unwrap());
        assert!(!worker.run_once().await.unwrap());
        let filled = input.handle_request(path, None).await;
        assert_eq!(filled.status, StatusCode::OK);
        assert_eq!(filled.body, Bytes::from_static(b"single queued fill"));
        assert_eq!(client.calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn queue_claim_is_exclusive_until_completion() {
        let store = MemoryArchiveStore::new();
        let key = parse_replay_path("/web/20200115103000id_/https://example.com/").unwrap();
        store.enqueue_fill(FillRequest::Replay(key)).await.unwrap();

        let first = store
            .claim_next_fill("first-worker", Duration::from_secs(30))
            .await
            .unwrap()
            .expect("first worker claims the queued fill");
        let second = store
            .claim_next_fill("second-worker", Duration::from_secs(30))
            .await
            .unwrap();

        assert!(second.is_none());
        store.complete_fill(&first).await.unwrap();
        assert!(
            store
                .claim_next_fill("second-worker", Duration::from_secs(30))
                .await
                .unwrap()
                .is_none()
        );
    }

    #[tokio::test]
    async fn retry_after_is_queued_for_metadata_and_replay() {
        for endpoint in [Endpoint::Availability, Endpoint::Replay] {
            let (path, query) = input_for_endpoint(endpoint);
            let store = Arc::new(MemoryArchiveStore::new());
            let mut headers = HeaderMap::new();
            headers.insert("retry-after", header_value("7"));
            let client = Arc::new(CountingClient::new(vec![upstream_response(
                StatusCode::SERVICE_UNAVAILABLE,
                headers,
                b"try later",
            )]));
            let availability = Arc::new(AdaptiveLimiter::new(LimiterConfig {
                queue_wait: Duration::from_millis(100),
                ..LimiterConfig::availability()
            }));
            let replay = Arc::new(AdaptiveLimiter::new(LimiterConfig {
                queue_wait: Duration::from_millis(100),
                ..LimiterConfig::replay()
            }));
            let limiter = match endpoint {
                Endpoint::Availability => availability.clone(),
                Endpoint::Replay => replay.clone(),
                Endpoint::Cdx => unreachable!("test only covers availability and replay"),
            };
            let worker_service = Arc::new(
                ArchiveService::new(store.clone(), client.clone()).with_endpoint_limiters(
                    availability,
                    Arc::new(AdaptiveLimiter::new(LimiterConfig::cdx())),
                    replay,
                ),
            );
            let (input, worker) =
                input_worker(&store, worker_service.clone(), Duration::from_millis(50));
            let input_task = tokio::spawn({
                let input = input.clone();
                async move { input.handle_request(path, query).await }
            });

            run_one_queued_fill(worker.clone()).await;
            let response = tokio::time::timeout(Duration::from_secs(1), input_task)
                .await
                .expect("input request should reach its queue timeout")
                .unwrap();

            assert_eq!(response.status, StatusCode::SERVICE_UNAVAILABLE);
            assert_eq!(
                response_header(&response, "retry-after").as_deref(),
                Some("30")
            );
            assert_not_cached(&store, endpoint, path, query).await;
            assert_eq!(limiter.snapshot().await.backoff_seconds, Some(7));
            assert!(worker_service.metrics().await.unwrap().contains(&format!(
                "wayback_cache_acquisition_failures_total{{endpoint=\"{}\",reason=\"upstream_retry_after\",status=\"503\"}} 1",
                endpoint.as_str()
            )));
            assert!(!worker.run_once().await.unwrap());
            assert_eq!(client.calls.load(Ordering::SeqCst), 1);
        }
    }

    #[tokio::test]
    async fn transient_status_is_not_cached_for_metadata_or_replay() {
        for endpoint in [Endpoint::Availability, Endpoint::Replay] {
            let (path, query) = input_for_endpoint(endpoint);
            let store = Arc::new(MemoryArchiveStore::new());
            let client = Arc::new(CountingClient::new(vec![upstream_response(
                StatusCode::BAD_GATEWAY,
                HeaderMap::new(),
                b"bad gateway",
            )]));
            let worker_service = Arc::new(ArchiveService::new(store.clone(), client.clone()));
            let (input, worker) =
                input_worker(&store, worker_service.clone(), Duration::from_millis(50));

            let response = serve_miss(input.clone(), worker.clone(), path, query).await;

            assert_eq!(response.status, StatusCode::SERVICE_UNAVAILABLE);
            assert_eq!(
                response_header(&response, "retry-after").as_deref(),
                Some("30")
            );
            assert_not_cached(&store, endpoint, path, query).await;
            assert!(worker_service.metrics().await.unwrap().contains(&format!(
                "wayback_cache_acquisition_failures_total{{endpoint=\"{}\",reason=\"upstream_transient_status\",status=\"502\"}} 1",
                endpoint.as_str()
            )));
            assert!(!worker.run_once().await.unwrap());
            assert_eq!(client.calls.load(Ordering::SeqCst), 1);
        }
    }

    #[tokio::test]
    async fn limiter_queue_timeout_is_recorded_by_worker() {
        let store = Arc::new(MemoryArchiveStore::new());
        let client = Arc::new(CountingClient::new(vec![upstream_response(
            StatusCode::OK,
            content_type_headers("text/html"),
            b"unused",
        )]));
        let replay_limiter = Arc::new(AdaptiveLimiter::new(LimiterConfig {
            initial: 1,
            min: 1,
            max: 1,
            queue_wait: Duration::from_millis(1),
            ..LimiterConfig::replay()
        }));
        let held = replay_limiter
            .clone()
            .acquire()
            .await
            .expect("test holds the replay limiter");
        let worker_service = Arc::new(
            ArchiveService::new(store.clone(), client.clone()).with_endpoint_limiters(
                Arc::new(AdaptiveLimiter::new(LimiterConfig::availability())),
                Arc::new(AdaptiveLimiter::new(LimiterConfig::cdx())),
                replay_limiter,
            ),
        );
        let key = parse_replay_path("/web/20200115103000id_/https://example.com/").unwrap();
        store.enqueue_fill(FillRequest::Replay(key)).await.unwrap();
        let worker = ArchiveFiller::new(store, worker_service.clone());

        assert!(worker.run_once().await.unwrap());
        assert_eq!(client.calls.load(Ordering::SeqCst), 0);
        assert!(worker_service.metrics().await.unwrap().contains(
            "wayback_cache_acquisition_failures_total{endpoint=\"replay\",reason=\"limiter_queue_timeout\",status=\"none\"} 1"
        ));
        drop(held);
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

        let started = tokio::time::Instant::now();
        let result = client
            .fetch_metadata(&request, DEFAULT_MAX_METADATA_BYTES)
            .await;

        assert!(result.is_err());
        assert!(started.elapsed() < Duration::from_secs(2));
        server.abort();
    }

    fn input_worker(
        store: &Arc<MemoryArchiveStore>,
        worker_service: Arc<ArchiveService>,
        queue_wait: Duration,
    ) -> (Arc<ArchiveInputService>, Arc<ArchiveFiller>) {
        (
            Arc::new(ArchiveInputService::new(store.clone()).with_queue_wait(queue_wait)),
            Arc::new(ArchiveFiller::new(store.clone(), worker_service)),
        )
    }

    fn input_for_endpoint(endpoint: Endpoint) -> (&'static str, Option<&'static str>) {
        match endpoint {
            Endpoint::Availability => (
                "/wayback/available",
                Some("url=https://example.com/&timestamp=20200101000000"),
            ),
            Endpoint::Replay => ("/web/20200115103000id_/https://example.com/", None),
            Endpoint::Cdx => unreachable!("test only covers availability and replay"),
        }
    }

    async fn assert_not_cached(
        store: &MemoryArchiveStore,
        endpoint: Endpoint,
        path: &str,
        query: Option<&str>,
    ) {
        match endpoint {
            Endpoint::Availability | Endpoint::Cdx => {
                let key = parse_metadata_request(path, query).unwrap().key;
                assert!(store.get_metadata(&key).await.unwrap().is_none());
            }
            Endpoint::Replay => {
                let key = parse_replay_path(path).unwrap();
                assert!(store.get_replay(&key).await.unwrap().is_none());
            }
        }
    }

    async fn serve_miss(
        input: Arc<ArchiveInputService>,
        worker: Arc<ArchiveFiller>,
        path: &'static str,
        query: Option<&'static str>,
    ) -> ArchiveResponse {
        let input_task = tokio::spawn(async move { input.handle_request(path, query).await });
        run_one_queued_fill(worker).await;
        tokio::time::timeout(Duration::from_secs(1), input_task)
            .await
            .expect("input request should complete after fill")
            .unwrap()
    }

    async fn run_one_queued_fill(worker: Arc<ArchiveFiller>) {
        tokio::time::timeout(Duration::from_secs(1), async {
            loop {
                if worker.run_once().await.expect("filler worker should run") {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("input should enqueue a fill for the worker");
    }

    struct CountingClient {
        calls: AtomicUsize,
        responses: Mutex<VecDeque<UpstreamResponse>>,
    }

    impl CountingClient {
        fn new(responses: Vec<UpstreamResponse>) -> Self {
            Self {
                calls: AtomicUsize::new(0),
                responses: Mutex::new(responses.into()),
            }
        }

        async fn pop_response(&self) -> Result<UpstreamResponse> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            Ok(self
                .responses
                .lock()
                .await
                .pop_front()
                .expect("test response should be configured"))
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
}
