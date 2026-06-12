mod ia_client;
mod limiter;
mod metrics;
mod path;
mod response;
mod service;
mod store;
mod types;
mod util;

pub use ia_client::{IaClient, ReqwestIaClient, UpstreamResponse, UpstreamTimeouts};
pub use limiter::{
    AcquisitionOutcome, AdaptiveLimiter, LimiterConfig, LimiterPermit, LimiterSnapshot,
};
pub use path::{parse_metadata_request, parse_replay_path};
pub use response::ArchiveResponse;
pub use service::ArchiveService;
pub use store::{
    ArchiveStore, BlobRef, BlobStore, MemoryArchiveStore, MemoryBlobStore, PostgresArchiveStore,
    S3BlobStore,
};
pub use types::{
    Endpoint, FillLeaseKey, MetadataKey, MetadataRecord, MetadataRequest, ReplayKey, ReplayRecord,
    StoredMetadata, StoredReplay,
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
    use std::time::{Duration, Instant};

    use anyhow::Result;
    use async_trait::async_trait;
    use bytes::Bytes;
    use http::header::CONTENT_TYPE;
    use http::{HeaderMap, HeaderValue};
    use tokio::sync::{Mutex, oneshot};

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
    async fn concurrent_identical_misses_share_fill_lease_across_services() {
        let (release_send, release_recv) = oneshot::channel();
        let client = Arc::new(BlockingClient {
            calls: AtomicUsize::new(0),
            release: Mutex::new(Some(release_recv)),
        });
        let store = Arc::new(MemoryArchiveStore::new());
        let first_service = Arc::new(ArchiveService::new(store.clone(), client.clone()));
        let second_service = Arc::new(ArchiveService::new(store, client.clone()));
        let path = "/web/20200115103000id_/https://example.com/";

        let first = tokio::spawn({
            let service = first_service.clone();
            async move { service.handle_path(path).await }
        });
        let second = tokio::spawn({
            let service = second_service.clone();
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
