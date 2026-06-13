use std::time::Duration;

use anyhow::Result;
use async_trait::async_trait;
use bytes::Bytes;

use crate::types::{
    FillLeaseKey, FillRequest, MetadataKey, ReplayKey, StoredMetadata, StoredReplay,
};

mod memory;
mod postgres;
mod s3;

pub use memory::{MemoryArchiveStore, MemoryBlobStore};
pub use postgres::PostgresArchiveStore;
pub use s3::S3BlobStore;

#[async_trait]
pub trait ArchiveStore: Send + Sync {
    async fn get_replay(&self, key: &ReplayKey) -> Result<Option<StoredReplay>>;
    async fn put_replay(&self, replay: StoredReplay) -> Result<()>;
    async fn get_metadata(&self, key: &MetadataKey) -> Result<Option<StoredMetadata>>;
    async fn put_metadata(&self, metadata: StoredMetadata) -> Result<()>;
    async fn enqueue_fill(&self, request: FillRequest) -> Result<()>;
    async fn claim_next_fill(&self, owner: &str, ttl: Duration) -> Result<Option<ClaimedFill>>;
    async fn complete_fill(&self, job: &ClaimedFill) -> Result<()>;
    async fn retry_fill(
        &self,
        job: &ClaimedFill,
        retry_after: Option<Duration>,
        status: Option<u16>,
        error: Option<&str>,
    ) -> Result<()>;
    async fn wait_for_fill_queue_change(&self, timeout: Duration) -> Result<bool>;
    async fn wait_for_fill_change(&self, key: &FillLeaseKey, timeout: Duration) -> Result<bool>;
    async fn try_acquire_fill_lease(
        &self,
        key: &FillLeaseKey,
        owner: &str,
        ttl: Duration,
    ) -> Result<bool>;
    async fn release_fill_lease(&self, key: &FillLeaseKey, owner: &str) -> Result<()>;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClaimedFill {
    pub request: FillRequest,
    pub owner: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BlobRef {
    pub key: String,
    pub sha256: String,
    pub size: usize,
}

#[async_trait]
pub trait BlobStore: Send + Sync {
    async fn put_body(&self, body: Bytes) -> Result<BlobRef>;
    async fn get_body(&self, key: &str) -> Result<Bytes>;
}

pub(crate) fn blob_key(sha256: &str) -> String {
    format!("sha256/{}/{}/{}", &sha256[..2], &sha256[2..4], sha256)
}
