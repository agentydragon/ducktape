use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use log::info;
use serde::Deserialize;

use crate::ia_client::UpstreamTimeouts;
use crate::store::{ArchiveStore, MemoryArchiveStore, PostgresArchiveStore, S3BlobStore};
use crate::{
    DEFAULT_AVAILABILITY_TIMEOUT, DEFAULT_CDX_TIMEOUT, DEFAULT_MAX_BODY_BYTES,
    DEFAULT_MAX_METADATA_BYTES, DEFAULT_MAX_QUEUE_WAIT, DEFAULT_REPLAY_TIMEOUT,
};

const DEFAULT_CONFIG_PATH: &str = "/etc/wayback-archive/config.yaml";
const DEFAULT_AUTH_TOKEN_ENV: &str = "WAYBACK_ARCHIVE_AUTH_TOKEN";
const DEFAULT_DATABASE_URL_ENV: &str = "WAYBACK_ARCHIVE_DATABASE_URL";
const DEFAULT_S3_ACCESS_KEY_ID_ENV: &str = "WAYBACK_ARCHIVE_S3_ACCESS_KEY_ID";
const DEFAULT_S3_SECRET_ACCESS_KEY_ENV: &str = "WAYBACK_ARCHIVE_S3_SECRET_ACCESS_KEY";

#[derive(Debug, Clone, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct ArchiveConfig {
    pub port: u16,
    pub auth_port: u16,
    pub auth_token_env: Option<String>,
    pub queue_wait_seconds: u64,
    pub upstream: UpstreamConfig,
    pub body_limits: BodyLimitConfig,
    pub store: StoreConfig,
}

impl Default for ArchiveConfig {
    fn default() -> Self {
        Self {
            port: 8080,
            auth_port: 8090,
            auth_token_env: Some(DEFAULT_AUTH_TOKEN_ENV.to_string()),
            queue_wait_seconds: DEFAULT_MAX_QUEUE_WAIT.as_secs(),
            upstream: UpstreamConfig::default(),
            body_limits: BodyLimitConfig::default(),
            store: StoreConfig::default(),
        }
    }
}

impl ArchiveConfig {
    pub fn settings(&self) -> ArchiveSettings {
        ArchiveSettings {
            port: self.port,
            auth_port: self.auth_port,
            auth_token: optional_env(self.auth_token_env.as_deref()),
            web_upstream: self.upstream.web.clone(),
            availability_upstream: self.upstream.availability.clone(),
            max_body_bytes: self.body_limits.replay_bytes,
            max_metadata_bytes: self.body_limits.metadata_bytes,
            queue_wait: Duration::from_secs(self.queue_wait_seconds),
            timeouts: UpstreamTimeouts {
                availability: Duration::from_secs(self.upstream.timeouts_seconds.availability),
                cdx: Duration::from_secs(self.upstream.timeouts_seconds.cdx),
                replay: Duration::from_secs(self.upstream.timeouts_seconds.replay),
            },
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct UpstreamConfig {
    pub web: String,
    pub availability: String,
    pub timeouts_seconds: UpstreamTimeoutSeconds,
}

impl Default for UpstreamConfig {
    fn default() -> Self {
        Self {
            web: "https://web.archive.org".to_string(),
            availability: "https://archive.org".to_string(),
            timeouts_seconds: UpstreamTimeoutSeconds::default(),
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct UpstreamTimeoutSeconds {
    pub availability: u64,
    pub cdx: u64,
    pub replay: u64,
}

impl Default for UpstreamTimeoutSeconds {
    fn default() -> Self {
        Self {
            availability: DEFAULT_AVAILABILITY_TIMEOUT.as_secs(),
            cdx: DEFAULT_CDX_TIMEOUT.as_secs(),
            replay: DEFAULT_REPLAY_TIMEOUT.as_secs(),
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct BodyLimitConfig {
    pub replay_bytes: usize,
    pub metadata_bytes: usize,
}

impl Default for BodyLimitConfig {
    fn default() -> Self {
        Self {
            replay_bytes: DEFAULT_MAX_BODY_BYTES,
            metadata_bytes: DEFAULT_MAX_METADATA_BYTES,
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct StoreConfig {
    pub database_url_env: Option<String>,
    pub s3: Option<S3Config>,
}

impl Default for StoreConfig {
    fn default() -> Self {
        Self {
            database_url_env: Some(DEFAULT_DATABASE_URL_ENV.to_string()),
            s3: None,
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct S3Config {
    pub endpoint: Option<String>,
    pub bucket: Option<String>,
    pub region: String,
    pub allow_http: bool,
    pub access_key_id_env: Option<String>,
    pub secret_access_key_env: Option<String>,
}

impl Default for S3Config {
    fn default() -> Self {
        Self {
            endpoint: None,
            bucket: None,
            region: "us-east-1".to_string(),
            allow_http: true,
            access_key_id_env: Some(DEFAULT_S3_ACCESS_KEY_ID_ENV.to_string()),
            secret_access_key_env: Some(DEFAULT_S3_SECRET_ACCESS_KEY_ENV.to_string()),
        }
    }
}

#[derive(Debug, Clone)]
pub struct ArchiveSettings {
    pub port: u16,
    pub auth_port: u16,
    pub auth_token: Option<String>,
    pub web_upstream: String,
    pub availability_upstream: String,
    pub max_body_bytes: usize,
    pub max_metadata_bytes: usize,
    pub queue_wait: Duration,
    pub timeouts: UpstreamTimeouts,
}

pub fn archive_config_from_env() -> Result<ArchiveConfig> {
    if let Some(path) = optional_env(Some("WAYBACK_ARCHIVE_CONFIG")).map(PathBuf::from) {
        return load_archive_config(&path);
    }

    let default_path = Path::new(DEFAULT_CONFIG_PATH);
    if default_path.exists() {
        return load_archive_config(default_path);
    }

    Ok(ArchiveConfig::default())
}

pub fn archive_settings_from_env() -> Result<ArchiveSettings> {
    Ok(archive_config_from_env()?.settings())
}

pub async fn archive_store_from_env() -> Result<Arc<dyn ArchiveStore>> {
    let config = archive_config_from_env()?;
    archive_store_from_config(&config).await
}

pub async fn archive_store_from_config(config: &ArchiveConfig) -> Result<Arc<dyn ArchiveStore>> {
    let Some(database_url) = optional_env(config.store.database_url_env.as_deref()) else {
        info!("using in-memory archive store");
        return Ok(Arc::new(MemoryArchiveStore::new()));
    };

    info!("using Postgres metadata store and S3 blob store");
    let s3 = config
        .store
        .s3
        .as_ref()
        .context("store.s3 must be configured when database_url_env is set")?;
    let blobs = Arc::new(S3BlobStore::new(
        s3.endpoint
            .clone()
            .context("store.s3.endpoint must be configured")?,
        s3.bucket
            .clone()
            .context("store.s3.bucket must be configured")?,
        required_env(
            s3.access_key_id_env.as_deref(),
            "store.s3.access_key_id_env",
        )?,
        required_env(
            s3.secret_access_key_env.as_deref(),
            "store.s3.secret_access_key_env",
        )?,
        s3.region.clone(),
        s3.allow_http,
    )?);
    Ok(Arc::new(
        PostgresArchiveStore::new(database_url, blobs).await?,
    ))
}

fn load_archive_config(path: &Path) -> Result<ArchiveConfig> {
    let raw = std::fs::read_to_string(path)
        .with_context(|| format!("reading archive config {}", path.display()))?;
    serde_yaml::from_str(&raw).with_context(|| format!("parsing archive config {}", path.display()))
}

fn optional_env(name: Option<&str>) -> Option<String> {
    name.and_then(|name| std::env::var(name).ok())
        .filter(|value| !value.is_empty())
}

fn required_env(name: Option<&str>, field: &str) -> Result<String> {
    let name = name.with_context(|| format!("{field} must be configured"))?;
    std::env::var(name).with_context(|| format!("{name} must be set"))
}
