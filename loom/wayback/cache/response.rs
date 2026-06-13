use std::time::Duration;

use bytes::Bytes;
use http::{HeaderMap, HeaderValue, StatusCode, header};

use crate::types::{Endpoint, StoredMetadata, StoredReplay};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ArchiveResponse {
    pub status: StatusCode,
    pub headers: HeaderMap,
    pub body: Bytes,
}

impl ArchiveResponse {
    pub(crate) fn text(status: StatusCode, text: impl Into<String>) -> Self {
        let mut headers = HeaderMap::new();
        headers.insert(
            header::CONTENT_TYPE,
            HeaderValue::from_static("text/plain; charset=utf-8"),
        );
        Self {
            status,
            headers,
            body: Bytes::from(text.into()),
        }
    }

    pub(crate) fn retry_after(endpoint: Endpoint) -> Self {
        Self::retry_after_duration(endpoint, None)
    }

    pub(crate) fn retry_after_duration(endpoint: Endpoint, duration: Option<Duration>) -> Self {
        let mut headers = HeaderMap::new();
        headers.insert(
            header::CONTENT_TYPE,
            HeaderValue::from_static("text/plain; charset=utf-8"),
        );
        headers.insert(
            header::RETRY_AFTER,
            HeaderValue::from_str(
                &retry_after_seconds(duration)
                    .unwrap_or_else(|| endpoint.retry_after_seconds())
                    .to_string(),
            )
            .expect("retry-after seconds must be a valid header value"),
        );
        Self {
            status: StatusCode::SERVICE_UNAVAILABLE,
            headers,
            body: Bytes::from("archive acquisition is backing off\n"),
        }
    }
}

pub(crate) fn stored_replay_response(replay: StoredReplay) -> ArchiveResponse {
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

pub(crate) fn stored_metadata_response(metadata: StoredMetadata) -> ArchiveResponse {
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

fn retry_after_seconds(duration: Option<Duration>) -> Option<u64> {
    duration.map(|duration| duration.as_secs() + u64::from(duration.subsec_nanos() > 0))
}
