use std::time::Duration;

use anyhow::{Result, anyhow};
use async_trait::async_trait;
use bytes::Bytes;
use http::HeaderMap;

use crate::types::{Endpoint, MetadataRequest, ReplayKey};
use crate::{DEFAULT_AVAILABILITY_TIMEOUT, DEFAULT_CDX_TIMEOUT, DEFAULT_REPLAY_TIMEOUT};

#[derive(Debug, Clone)]
pub struct UpstreamResponse {
    pub status: http::StatusCode,
    pub headers: HeaderMap,
    pub body: Bytes,
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
                .user_agent("ducktape-wayback-cache/1 (+agentydragon@gmail.com)")
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
        let status = response.status();
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
        let status = response.status();
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
