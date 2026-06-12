use std::sync::Arc;

use anyhow::{Context, Result};
use async_trait::async_trait;
use bytes::Bytes;
use object_store::aws::AmazonS3Builder;
use object_store::path::Path as ObjectPath;
use object_store::{ObjectStore, ObjectStoreExt, PutPayload};

use crate::store::{BlobRef, BlobStore, blob_key};
use crate::util::sha256_hex;

pub struct S3BlobStore {
    store: Arc<dyn ObjectStore>,
}

impl S3BlobStore {
    pub fn new(
        endpoint: String,
        bucket: String,
        access_key_id: String,
        secret_access_key: String,
        region: String,
        allow_http: bool,
    ) -> Result<Self> {
        let store = AmazonS3Builder::new()
            .with_endpoint(endpoint)
            .with_bucket_name(bucket)
            .with_access_key_id(access_key_id)
            .with_secret_access_key(secret_access_key)
            .with_region(region)
            .with_allow_http(allow_http)
            .with_virtual_hosted_style_request(false)
            .build()?;
        Ok(Self {
            store: Arc::new(store),
        })
    }

    pub fn from_env() -> Result<Self> {
        Self::new(
            required_env("WAYBACK_ARCHIVE_S3_ENDPOINT")?,
            required_env("WAYBACK_ARCHIVE_S3_BUCKET")?,
            required_env("WAYBACK_ARCHIVE_S3_ACCESS_KEY_ID")?,
            required_env("WAYBACK_ARCHIVE_S3_SECRET_ACCESS_KEY")?,
            std::env::var("WAYBACK_ARCHIVE_S3_REGION").unwrap_or_else(|_| "us-east-1".to_string()),
            std::env::var("WAYBACK_ARCHIVE_S3_ALLOW_HTTP")
                .map_or(true, |value| value == "1" || value == "true"),
        )
    }
}

#[async_trait]
impl BlobStore for S3BlobStore {
    async fn put_body(&self, body: Bytes) -> Result<BlobRef> {
        let sha256 = sha256_hex(&body);
        let key = blob_key(&sha256);
        self.store
            .put(
                &ObjectPath::from(key.as_str()),
                PutPayload::from_bytes(body.clone()),
            )
            .await
            .with_context(|| format!("putting replay blob {key}"))?;
        Ok(BlobRef {
            key,
            sha256,
            size: body.len(),
        })
    }

    async fn get_body(&self, key: &str) -> Result<Bytes> {
        Ok(self
            .store
            .get(&ObjectPath::from(key))
            .await
            .with_context(|| format!("getting replay blob {key}"))?
            .bytes()
            .await?)
    }
}

fn required_env(name: &str) -> Result<String> {
    std::env::var(name).with_context(|| format!("{name} must be set"))
}
