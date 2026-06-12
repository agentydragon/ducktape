use std::time::Duration;

use bytes::Bytes;
use http::StatusCode;

use crate::types::Endpoint;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ArchiveResponse {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Bytes,
}

impl ArchiveResponse {
    pub(crate) fn text(status: StatusCode, text: impl Into<String>) -> Self {
        Self {
            status: status.as_u16(),
            headers: vec![(
                "content-type".to_string(),
                "text/plain; charset=utf-8".to_string(),
            )],
            body: Bytes::from(text.into()),
        }
    }

    pub(crate) fn retry_after(endpoint: Endpoint) -> Self {
        Self::retry_after_duration(endpoint, None)
    }

    pub(crate) fn retry_after_duration(endpoint: Endpoint, duration: Option<Duration>) -> Self {
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

fn retry_after_seconds(duration: Option<Duration>) -> Option<u64> {
    duration.map(|duration| duration.as_secs() + u64::from(duration.subsec_nanos() > 0))
}
