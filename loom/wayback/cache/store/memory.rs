use std::collections::HashMap;
use std::time::{Duration, Instant};

use anyhow::{Result, anyhow};
use async_trait::async_trait;
use bytes::Bytes;
use tokio::sync::{Mutex, Notify};
use tokio::time::timeout;

use crate::store::{ArchiveStore, BlobRef, BlobStore, ClaimedFill, blob_key};
use crate::types::{
    FillLeaseKey, FillRequest, MetadataKey, ReplayKey, StoredMetadata, StoredReplay,
};
use crate::util::sha256_hex;

#[derive(Default)]
pub struct MemoryArchiveStore {
    replays: Mutex<HashMap<ReplayKey, StoredReplay>>,
    metadata: Mutex<HashMap<MetadataKey, StoredMetadata>>,
    leases: Mutex<HashMap<FillLeaseKey, MemoryLease>>,
    fills: Mutex<HashMap<FillLeaseKey, MemoryFill>>,
    fill_notify: Notify,
}

#[derive(Debug, Clone)]
struct MemoryLease {
    owner: String,
    expires_at: Instant,
}

#[derive(Debug, Clone)]
struct MemoryFill {
    request: FillRequest,
    lease: Option<MemoryLease>,
    next_attempt: Instant,
}

impl MemoryArchiveStore {
    pub fn new() -> Self {
        Self::default()
    }
}

#[async_trait]
impl ArchiveStore for MemoryArchiveStore {
    async fn get_replay(&self, key: &ReplayKey) -> Result<Option<StoredReplay>> {
        Ok(self.replays.lock().await.get(key).cloned())
    }

    async fn put_replay(&self, replay: StoredReplay) -> Result<()> {
        let key = match &replay {
            StoredReplay::Capture(record) => record.key.clone(),
            StoredReplay::BodyTooLarge { key, .. } => key.clone(),
        };
        self.replays.lock().await.insert(key, replay);
        Ok(())
    }

    async fn get_metadata(&self, key: &MetadataKey) -> Result<Option<StoredMetadata>> {
        Ok(self.metadata.lock().await.get(key).cloned())
    }

    async fn put_metadata(&self, metadata: StoredMetadata) -> Result<()> {
        let key = match &metadata {
            StoredMetadata::Response(record) => record.key.clone(),
            StoredMetadata::BodyTooLarge { key, .. } => key.clone(),
        };
        self.metadata.lock().await.insert(key, metadata);
        Ok(())
    }

    async fn enqueue_fill(&self, request: FillRequest) -> Result<()> {
        let key = request.lease_key();
        self.fills.lock().await.entry(key).or_insert(MemoryFill {
            request,
            lease: None,
            next_attempt: Instant::now(),
        });
        self.fill_notify.notify_waiters();
        Ok(())
    }

    async fn claim_next_fill(&self, owner: &str, ttl: Duration) -> Result<Option<ClaimedFill>> {
        let mut fills = self.fills.lock().await;
        let now = Instant::now();
        let Some(fill) = fills.values_mut().find(|fill| {
            fill.next_attempt <= now
                && fill
                    .lease
                    .as_ref()
                    .is_none_or(|lease| lease.expires_at <= now)
        }) else {
            return Ok(None);
        };
        fill.lease = Some(MemoryLease {
            owner: owner.to_string(),
            expires_at: now + ttl,
        });
        Ok(Some(ClaimedFill {
            request: fill.request.clone(),
            owner: owner.to_string(),
        }))
    }

    async fn complete_fill(&self, job: &ClaimedFill) -> Result<()> {
        let mut fills = self.fills.lock().await;
        let key = job.request.lease_key();
        if fills
            .get(&key)
            .and_then(|fill| fill.lease.as_ref())
            .is_some_and(|lease| lease.owner == job.owner)
        {
            fills.remove(&key);
            self.fill_notify.notify_waiters();
        }
        Ok(())
    }

    async fn retry_fill(
        &self,
        job: &ClaimedFill,
        retry_after: Option<Duration>,
        _status: Option<u16>,
        _error: Option<&str>,
    ) -> Result<()> {
        let mut fills = self.fills.lock().await;
        let key = job.request.lease_key();
        if let Some(fill) = fills.get_mut(&key)
            && fill
                .lease
                .as_ref()
                .is_some_and(|lease| lease.owner == job.owner)
        {
            fill.lease = None;
            fill.next_attempt = Instant::now()
                + retry_after.unwrap_or_else(|| {
                    Duration::from_secs(job.request.endpoint().retry_after_seconds())
                });
            self.fill_notify.notify_waiters();
        }
        Ok(())
    }

    async fn wait_for_fill_change(&self, _key: &FillLeaseKey, wait: Duration) -> Result<bool> {
        Ok(timeout(wait, self.fill_notify.notified()).await.is_ok())
    }

    async fn wait_for_fill_queue_change(&self, wait: Duration) -> Result<bool> {
        Ok(timeout(wait, self.fill_notify.notified()).await.is_ok())
    }

    async fn try_acquire_fill_lease(
        &self,
        key: &FillLeaseKey,
        owner: &str,
        ttl: Duration,
    ) -> Result<bool> {
        let mut leases = self.leases.lock().await;
        let now = Instant::now();
        if leases
            .get(key)
            .is_some_and(|lease| lease.expires_at > now && lease.owner != owner)
        {
            return Ok(false);
        }
        leases.insert(
            key.clone(),
            MemoryLease {
                owner: owner.to_string(),
                expires_at: now + ttl,
            },
        );
        Ok(true)
    }

    async fn release_fill_lease(&self, key: &FillLeaseKey, owner: &str) -> Result<()> {
        let mut leases = self.leases.lock().await;
        if leases.get(key).is_some_and(|lease| lease.owner == owner) {
            leases.remove(key);
        }
        Ok(())
    }
}

#[derive(Default)]
pub struct MemoryBlobStore {
    blobs: Mutex<HashMap<String, Bytes>>,
}

impl MemoryBlobStore {
    pub fn new() -> Self {
        Self::default()
    }
}

#[async_trait]
impl BlobStore for MemoryBlobStore {
    async fn put_body(&self, body: Bytes) -> Result<BlobRef> {
        let sha256 = sha256_hex(&body);
        let key = blob_key(&sha256);
        self.blobs.lock().await.insert(key.clone(), body.clone());
        Ok(BlobRef {
            key,
            sha256,
            size: body.len(),
        })
    }

    async fn get_body(&self, key: &str) -> Result<Bytes> {
        self.blobs
            .lock()
            .await
            .get(key)
            .cloned()
            .ok_or_else(|| anyhow!("blob missing: {key}"))
    }
}
