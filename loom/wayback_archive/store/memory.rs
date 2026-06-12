use std::collections::HashMap;

use anyhow::{Result, anyhow};
use async_trait::async_trait;
use bytes::Bytes;
use tokio::sync::Mutex;

use crate::store::{ArchiveStore, BlobRef, BlobStore, blob_key};
use crate::types::{MetadataKey, ReplayKey, StoredMetadata, StoredReplay};
use crate::util::sha256_hex;

#[derive(Default)]
pub struct MemoryArchiveStore {
    replays: Mutex<HashMap<ReplayKey, StoredReplay>>,
    metadata: Mutex<HashMap<MetadataKey, StoredMetadata>>,
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
