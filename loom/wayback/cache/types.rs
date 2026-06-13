use std::fmt::{Display, Formatter};
use std::time::Duration;

use bytes::Bytes;
use http::{HeaderMap, StatusCode};
use serde::{Deserialize, Serialize};

use crate::RETRY_AFTER_SECONDS;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Endpoint {
    Availability,
    Cdx,
    Replay,
}

impl Endpoint {
    pub(crate) const ALL: [Endpoint; 3] = [Endpoint::Availability, Endpoint::Cdx, Endpoint::Replay];

    pub(crate) fn retry_after_seconds(self) -> u64 {
        match self {
            Endpoint::Availability => 5,
            Endpoint::Cdx | Endpoint::Replay => RETRY_AFTER_SECONDS,
        }
    }

    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Endpoint::Availability => "availability",
            Endpoint::Cdx => "cdx",
            Endpoint::Replay => "replay",
        }
    }

    pub(crate) fn from_str(value: &str) -> Option<Self> {
        match value {
            "availability" => Some(Endpoint::Availability),
            "cdx" => Some(Endpoint::Cdx),
            "replay" => Some(Endpoint::Replay),
            _ => None,
        }
    }

    pub(crate) fn metadata_path(self) -> Option<&'static str> {
        match self {
            Endpoint::Availability => Some("/wayback/available"),
            Endpoint::Cdx => Some("/cdx/search/cdx"),
            Endpoint::Replay => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct ReplayKey {
    pub capture_ts: String,
    pub modifier: String,
    pub original_url: String,
}

impl ReplayKey {
    pub(crate) fn ia_path(&self) -> String {
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

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct FillLeaseKey {
    pub endpoint: Endpoint,
    pub key: String,
}

impl FillLeaseKey {
    pub(crate) fn replay(key: &ReplayKey) -> Self {
        Self {
            endpoint: Endpoint::Replay,
            key: key.to_string(),
        }
    }

    pub(crate) fn metadata(key: &MetadataKey) -> Self {
        Self {
            endpoint: key.endpoint,
            key: key.normalized_query.clone(),
        }
    }
}

impl Display for FillLeaseKey {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}:{}", self.endpoint.as_str(), self.key)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum FillRequest {
    Metadata(MetadataRequest),
    Replay(ReplayKey),
}

impl FillRequest {
    pub(crate) fn endpoint(&self) -> Endpoint {
        match self {
            FillRequest::Metadata(request) => request.key.endpoint,
            FillRequest::Replay(_) => Endpoint::Replay,
        }
    }

    pub(crate) fn lease_key(&self) -> FillLeaseKey {
        match self {
            FillRequest::Metadata(request) => FillLeaseKey::metadata(&request.key),
            FillRequest::Replay(key) => FillLeaseKey::replay(key),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FillAttemptResult {
    Completed,
    RetryAfter {
        retry_after: Option<Duration>,
        status: Option<u16>,
    },
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
    pub status: StatusCode,
    pub headers: HeaderMap,
    pub body: Bytes,
    pub blob_key: Option<String>,
    pub sha256: String,
    pub body_size: usize,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct MetadataKey {
    pub endpoint: Endpoint,
    pub normalized_query: String,
}

impl Display for MetadataKey {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}?{}", self.endpoint.as_str(), self.normalized_query)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
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
    pub status: StatusCode,
    pub headers: HeaderMap,
    pub body: Bytes,
    pub sha256: String,
    pub body_size: usize,
}
