use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Result, anyhow};
use async_trait::async_trait;
use bytes::Bytes;
use http::{HeaderMap, HeaderName, HeaderValue, StatusCode};
use log::{info, warn};
use sea_orm::entity::prelude::*;
use sea_orm::sea_query::{Expr, OnConflict};
use sea_orm::sqlx::postgres::PgListener;
use sea_orm::{
    ColumnTrait, Condition, ConnectionTrait, Database, DatabaseConnection, DbBackend, EntityTrait,
    QueryFilter, QueryOrder, Schema, Set, Statement, TryInsertResult,
};
use serde_json::Value;
use tokio::sync::broadcast;
use tokio::time::timeout;

use crate::store::{ArchiveStore, BlobStore, ClaimedFill};
use crate::types::{
    Endpoint, FillLeaseKey, FillRequest, MetadataKey, MetadataRecord, ReplayKey, ReplayRecord,
    StoredMetadata, StoredReplay,
};

const FILL_NOTIFY_CHANNEL: &str = "wayback_fill_changed";

mod replay_record {
    use sea_orm::entity::prelude::*;

    #[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
    #[sea_orm(table_name = "wayback_replay_records")]
    pub struct Model {
        #[sea_orm(primary_key, auto_increment = false)]
        pub capture_ts: String,
        #[sea_orm(primary_key, auto_increment = false)]
        pub modifier: String,
        #[sea_orm(primary_key, auto_increment = false)]
        pub canonical_original_url: String,
        pub status: Option<i32>,
        pub headers: Json,
        pub blob_key: Option<String>,
        pub sha256: Option<String>,
        pub body_size: Option<i64>,
        pub classification: String,
        pub observed_size: Option<i64>,
    }

    #[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
    pub enum Relation {}

    impl ActiveModelBehavior for ActiveModel {}
}

mod metadata_record {
    use sea_orm::entity::prelude::*;

    #[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
    #[sea_orm(table_name = "wayback_metadata_records")]
    pub struct Model {
        #[sea_orm(primary_key, auto_increment = false)]
        pub endpoint: String,
        #[sea_orm(primary_key, auto_increment = false)]
        pub normalized_query: String,
        pub status: Option<i32>,
        pub headers: Json,
        pub body: Option<Vec<u8>>,
        pub sha256: Option<String>,
        pub body_size: Option<i64>,
        pub classification: String,
        pub observed_size: Option<i64>,
    }

    #[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
    pub enum Relation {}

    impl ActiveModelBehavior for ActiveModel {}
}

mod fill_lease {
    use sea_orm::entity::prelude::*;

    #[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
    #[sea_orm(table_name = "wayback_fill_leases")]
    pub struct Model {
        #[sea_orm(primary_key, auto_increment = false)]
        pub endpoint: String,
        #[sea_orm(primary_key, auto_increment = false)]
        pub lease_key: String,
        pub owner: String,
        pub expires_at_ms: i64,
    }

    #[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
    pub enum Relation {}

    impl ActiveModelBehavior for ActiveModel {}
}

mod fill_queue {
    use sea_orm::entity::prelude::*;

    #[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
    #[sea_orm(table_name = "wayback_fill_queue")]
    pub struct Model {
        #[sea_orm(primary_key, auto_increment = false)]
        pub endpoint: String,
        #[sea_orm(primary_key, auto_increment = false)]
        pub fill_key: String,
        pub request: Json,
        pub attempts: i32,
        pub next_attempt_at_ms: i64,
        pub lease_owner: Option<String>,
        pub lease_expires_at_ms: Option<i64>,
        pub last_status: Option<i32>,
        pub last_error: Option<String>,
        pub updated_at_ms: i64,
    }

    #[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
    pub enum Relation {}

    impl ActiveModelBehavior for ActiveModel {}
}

pub struct PostgresArchiveStore {
    db: DatabaseConnection,
    blobs: Arc<dyn BlobStore>,
    notifications: broadcast::Sender<String>,
}

impl PostgresArchiveStore {
    pub async fn new(database_url: String, blobs: Arc<dyn BlobStore>) -> Result<Self> {
        let db = Database::connect(database_url).await?;
        let (notifications, _) = broadcast::channel(1024);
        let store = Self {
            db,
            blobs,
            notifications,
        };
        store.migrate().await?;
        store.spawn_notification_listener();
        Ok(store)
    }

    async fn migrate(&self) -> Result<()> {
        let schema = Schema::new(DbBackend::Postgres);
        let create_table = schema
            .create_table_from_entity(replay_record::Entity)
            .if_not_exists()
            .to_owned();
        self.db
            .execute(self.db.get_database_backend().build(&create_table))
            .await?;
        let create_table = schema
            .create_table_from_entity(metadata_record::Entity)
            .if_not_exists()
            .to_owned();
        self.db
            .execute(self.db.get_database_backend().build(&create_table))
            .await?;
        let create_table = schema
            .create_table_from_entity(fill_lease::Entity)
            .if_not_exists()
            .to_owned();
        self.db
            .execute(self.db.get_database_backend().build(&create_table))
            .await?;
        let create_table = schema
            .create_table_from_entity(fill_queue::Entity)
            .if_not_exists()
            .to_owned();
        self.db
            .execute(self.db.get_database_backend().build(&create_table))
            .await?;
        Ok(())
    }

    fn spawn_notification_listener(&self) {
        let pool = self.db.get_postgres_connection_pool().clone();
        let sender = self.notifications.clone();
        tokio::spawn(async move {
            loop {
                match PgListener::connect_with(&pool).await {
                    Ok(mut listener) => {
                        listener.ignore_pool_close_event(true);
                        if let Err(error) = listener.listen(FILL_NOTIFY_CHANNEL).await {
                            warn!(
                                "Postgres LISTEN setup failed channel={FILL_NOTIFY_CHANNEL} error={error:#}"
                            );
                            tokio::time::sleep(Duration::from_secs(1)).await;
                            continue;
                        }
                        info!("listening for Postgres fill notifications");
                        loop {
                            match listener.recv().await {
                                Ok(notification) => {
                                    let _ = sender.send(notification.payload().to_string());
                                }
                                Err(error) => {
                                    warn!("Postgres notification receive failed error={error:#}");
                                    break;
                                }
                            }
                        }
                    }
                    Err(error) => {
                        warn!("Postgres LISTEN connection failed error={error:#}");
                    }
                }
                tokio::time::sleep(Duration::from_secs(1)).await;
            }
        });
    }

    async fn notify_fill_changed(&self, key: &FillLeaseKey) -> Result<()> {
        self.db
            .execute(Statement::from_sql_and_values(
                DbBackend::Postgres,
                "SELECT pg_notify($1, $2)",
                [
                    FILL_NOTIFY_CHANNEL.to_string().into(),
                    key.to_string().into(),
                ],
            ))
            .await?;
        let _ = self.notifications.send(key.to_string());
        Ok(())
    }
}

#[async_trait]
impl ArchiveStore for PostgresArchiveStore {
    async fn get_replay(&self, key: &ReplayKey) -> Result<Option<StoredReplay>> {
        let row = replay_record::Entity::find()
            .filter(
                Condition::all()
                    .add(replay_record::Column::CaptureTs.eq(key.capture_ts.clone()))
                    .add(replay_record::Column::Modifier.eq(key.modifier.clone()))
                    .add(replay_record::Column::CanonicalOriginalUrl.eq(key.original_url.clone())),
            )
            .one(&self.db)
            .await?;
        let Some(row) = row else {
            return Ok(None);
        };
        if row.classification == "body_too_large" {
            return Ok(Some(StoredReplay::BodyTooLarge {
                key: key.clone(),
                observed_size: row.observed_size.unwrap_or(0).try_into().unwrap_or(0),
            }));
        }

        let blob_key = row
            .blob_key
            .ok_or_else(|| anyhow!("replay row missing blob_key for {key}"))?;
        let body = self.blobs.get_body(&blob_key).await?;
        Ok(Some(StoredReplay::Capture(ReplayRecord {
            key: key.clone(),
            status: stored_status(
                row.status
                    .ok_or_else(|| anyhow!("replay row missing status for {key}"))?,
            )?,
            headers: headers_from_json(row.headers)?,
            blob_key: Some(blob_key),
            sha256: row
                .sha256
                .ok_or_else(|| anyhow!("replay row missing sha256 for {key}"))?,
            body_size: row
                .body_size
                .unwrap_or(body.len() as i64)
                .try_into()
                .unwrap_or(body.len()),
            body,
        })))
    }

    async fn put_replay(&self, replay: StoredReplay) -> Result<()> {
        match replay {
            StoredReplay::Capture(mut record) => {
                let blob = self.blobs.put_body(record.body.clone()).await?;
                record.blob_key = Some(blob.key.clone());
                record.sha256 = blob.sha256.clone();
                record.body_size = blob.size;
                let classification =
                    if record.status.is_client_error() || record.status.is_server_error() {
                        "archived_error"
                    } else {
                        "served_capture"
                    };
                let active = replay_record::ActiveModel {
                    capture_ts: Set(record.key.capture_ts),
                    modifier: Set(record.key.modifier),
                    canonical_original_url: Set(record.key.original_url),
                    status: Set(Some(i32::from(record.status.as_u16()))),
                    headers: Set(headers_to_json(&record.headers)?),
                    blob_key: Set(Some(blob.key)),
                    sha256: Set(Some(blob.sha256)),
                    body_size: Set(Some(blob.size as i64)),
                    classification: Set(classification.to_string()),
                    observed_size: Set(None),
                };
                replay_record::Entity::insert(active)
                    .on_conflict(replay_upsert_conflict())
                    .exec(&self.db)
                    .await?;
            }
            StoredReplay::BodyTooLarge { key, observed_size } => {
                let active = replay_record::ActiveModel {
                    capture_ts: Set(key.capture_ts),
                    modifier: Set(key.modifier),
                    canonical_original_url: Set(key.original_url),
                    status: Set(None),
                    headers: Set(Value::Array(Vec::new())),
                    blob_key: Set(None),
                    sha256: Set(None),
                    body_size: Set(None),
                    classification: Set("body_too_large".to_string()),
                    observed_size: Set(Some(observed_size as i64)),
                };
                replay_record::Entity::insert(active)
                    .on_conflict(replay_upsert_conflict())
                    .exec(&self.db)
                    .await?;
            }
        }
        Ok(())
    }

    async fn get_metadata(&self, key: &MetadataKey) -> Result<Option<StoredMetadata>> {
        let row = metadata_record::Entity::find()
            .filter(
                Condition::all()
                    .add(metadata_record::Column::Endpoint.eq(key.endpoint.as_str()))
                    .add(metadata_record::Column::NormalizedQuery.eq(key.normalized_query.clone())),
            )
            .one(&self.db)
            .await?;
        let Some(row) = row else {
            return Ok(None);
        };
        let endpoint = metadata_endpoint_from_str(&row.endpoint)
            .ok_or_else(|| anyhow!("metadata row has unknown endpoint {}", row.endpoint))?;
        let key = MetadataKey {
            endpoint,
            normalized_query: row.normalized_query,
        };
        if row.classification == "body_too_large" {
            return Ok(Some(StoredMetadata::BodyTooLarge {
                key,
                observed_size: row.observed_size.unwrap_or(0).try_into().unwrap_or(0),
            }));
        }

        let body = row
            .body
            .ok_or_else(|| anyhow!("metadata row missing body for {key}"))?;
        Ok(Some(StoredMetadata::Response(MetadataRecord {
            key,
            status: stored_status(
                row.status
                    .ok_or_else(|| anyhow!("metadata row missing status"))?,
            )?,
            headers: headers_from_json(row.headers)?,
            sha256: row
                .sha256
                .ok_or_else(|| anyhow!("metadata row missing sha256"))?,
            body_size: row
                .body_size
                .unwrap_or(body.len() as i64)
                .try_into()
                .unwrap_or(body.len()),
            body: Bytes::from(body),
        })))
    }

    async fn put_metadata(&self, metadata: StoredMetadata) -> Result<()> {
        match metadata {
            StoredMetadata::Response(record) => {
                let active = metadata_record::ActiveModel {
                    endpoint: Set(record.key.endpoint.as_str().to_string()),
                    normalized_query: Set(record.key.normalized_query),
                    status: Set(Some(i32::from(record.status.as_u16()))),
                    headers: Set(headers_to_json(&record.headers)?),
                    body: Set(Some(record.body.to_vec())),
                    sha256: Set(Some(record.sha256)),
                    body_size: Set(Some(record.body_size as i64)),
                    classification: Set("served_metadata".to_string()),
                    observed_size: Set(None),
                };
                metadata_record::Entity::insert(active)
                    .on_conflict(metadata_upsert_conflict())
                    .exec(&self.db)
                    .await?;
            }
            StoredMetadata::BodyTooLarge { key, observed_size } => {
                let active = metadata_record::ActiveModel {
                    endpoint: Set(key.endpoint.as_str().to_string()),
                    normalized_query: Set(key.normalized_query),
                    status: Set(None),
                    headers: Set(Value::Array(Vec::new())),
                    body: Set(None),
                    sha256: Set(None),
                    body_size: Set(None),
                    classification: Set("body_too_large".to_string()),
                    observed_size: Set(Some(observed_size as i64)),
                };
                metadata_record::Entity::insert(active)
                    .on_conflict(metadata_upsert_conflict())
                    .exec(&self.db)
                    .await?;
            }
        }
        Ok(())
    }

    async fn enqueue_fill(&self, request: FillRequest) -> Result<()> {
        let key = request.lease_key();
        let now_ms = epoch_ms();
        let active = fill_queue::ActiveModel {
            endpoint: Set(key.endpoint.as_str().to_string()),
            fill_key: Set(key.key.clone()),
            request: Set(serde_json::to_value(&request)?),
            attempts: Set(0),
            next_attempt_at_ms: Set(now_ms),
            lease_owner: Set(None),
            lease_expires_at_ms: Set(None),
            last_status: Set(None),
            last_error: Set(None),
            updated_at_ms: Set(now_ms),
        };
        let mut conflict =
            OnConflict::columns([fill_queue::Column::Endpoint, fill_queue::Column::FillKey]);
        conflict.do_nothing();
        fill_queue::Entity::insert(active)
            .on_conflict(conflict.to_owned())
            .do_nothing()
            .exec(&self.db)
            .await?;
        self.notify_fill_changed(&key).await?;
        Ok(())
    }

    async fn claim_next_fill(&self, owner: &str, ttl: Duration) -> Result<Option<ClaimedFill>> {
        for _ in 0..5 {
            let now_ms = epoch_ms();
            let Some(row) = fill_queue::Entity::find()
                .filter(fill_due_condition(now_ms))
                .order_by_asc(fill_queue::Column::NextAttemptAtMs)
                .one(&self.db)
                .await?
            else {
                return Ok(None);
            };
            let updated = fill_queue::Entity::update_many()
                .col_expr(
                    fill_queue::Column::LeaseOwner,
                    Expr::value(owner.to_string()),
                )
                .col_expr(
                    fill_queue::Column::LeaseExpiresAtMs,
                    Expr::value(now_ms + duration_ms(ttl)),
                )
                .col_expr(fill_queue::Column::UpdatedAtMs, Expr::value(now_ms))
                .filter(fill_row_condition(&row.endpoint, &row.fill_key))
                .filter(fill_due_condition(now_ms))
                .exec(&self.db)
                .await?;
            if updated.rows_affected == 0 {
                continue;
            }
            let request = serde_json::from_value(row.request)?;
            return Ok(Some(ClaimedFill {
                request,
                owner: owner.to_string(),
            }));
        }
        Ok(None)
    }

    async fn complete_fill(&self, job: &ClaimedFill) -> Result<()> {
        let key = job.request.lease_key();
        fill_queue::Entity::delete_many()
            .filter(fill_row_condition(key.endpoint.as_str(), &key.key))
            .filter(fill_queue::Column::LeaseOwner.eq(job.owner.clone()))
            .exec(&self.db)
            .await?;
        self.notify_fill_changed(&key).await?;
        Ok(())
    }

    async fn retry_fill(
        &self,
        job: &ClaimedFill,
        retry_after: Option<Duration>,
        status: Option<u16>,
        error: Option<&str>,
    ) -> Result<()> {
        let key = job.request.lease_key();
        let now_ms = epoch_ms();
        let retry_after = retry_after
            .unwrap_or_else(|| Duration::from_secs(job.request.endpoint().retry_after_seconds()));
        fill_queue::Entity::update_many()
            .col_expr(
                fill_queue::Column::Attempts,
                Expr::col(fill_queue::Column::Attempts).add(1),
            )
            .col_expr(
                fill_queue::Column::NextAttemptAtMs,
                Expr::value(now_ms + duration_ms(retry_after)),
            )
            .col_expr(
                fill_queue::Column::LeaseOwner,
                Expr::value(Option::<String>::None),
            )
            .col_expr(
                fill_queue::Column::LeaseExpiresAtMs,
                Expr::value(Option::<i64>::None),
            )
            .col_expr(
                fill_queue::Column::LastStatus,
                Expr::value(status.map(i32::from)),
            )
            .col_expr(
                fill_queue::Column::LastError,
                Expr::value(error.map(str::to_string)),
            )
            .col_expr(fill_queue::Column::UpdatedAtMs, Expr::value(now_ms))
            .filter(fill_row_condition(key.endpoint.as_str(), &key.key))
            .filter(fill_queue::Column::LeaseOwner.eq(job.owner.clone()))
            .exec(&self.db)
            .await?;
        self.notify_fill_changed(&key).await?;
        Ok(())
    }

    async fn wait_for_fill_queue_change(&self, wait: Duration) -> Result<bool> {
        let mut receiver = self.notifications.subscribe();
        let notified = async move {
            matches!(
                receiver.recv().await,
                Ok(_) | Err(broadcast::error::RecvError::Lagged(_))
            )
        };
        Ok(timeout(wait, notified).await.unwrap_or(false))
    }

    async fn wait_for_fill_change(&self, key: &FillLeaseKey, wait: Duration) -> Result<bool> {
        let key = key.to_string();
        let mut receiver = self.notifications.subscribe();
        let notified = async move {
            loop {
                match receiver.recv().await {
                    Ok(payload) if payload == key => return true,
                    Ok(_) => {}
                    Err(broadcast::error::RecvError::Lagged(_)) => return true,
                    Err(broadcast::error::RecvError::Closed) => return false,
                }
            }
        };
        Ok(timeout(wait, notified).await.unwrap_or(false))
    }

    async fn try_acquire_fill_lease(
        &self,
        key: &FillLeaseKey,
        owner: &str,
        ttl: Duration,
    ) -> Result<bool> {
        let now_ms = epoch_ms();
        let expires_at_ms = now_ms + duration_ms(ttl);
        let endpoint = key.endpoint.as_str().to_string();
        let lease_key = key.key.clone();

        let updated = fill_lease::Entity::update_many()
            .col_expr(fill_lease::Column::Owner, Expr::value(owner.to_string()))
            .col_expr(fill_lease::Column::ExpiresAtMs, Expr::value(expires_at_ms))
            .filter(
                Condition::all()
                    .add(fill_lease::Column::Endpoint.eq(endpoint.clone()))
                    .add(fill_lease::Column::LeaseKey.eq(lease_key.clone()))
                    .add(fill_lease::Column::ExpiresAtMs.lte(now_ms)),
            )
            .exec(&self.db)
            .await?;
        if updated.rows_affected > 0 {
            return Ok(true);
        }

        let active = fill_lease::ActiveModel {
            endpoint: Set(endpoint),
            lease_key: Set(lease_key),
            owner: Set(owner.to_string()),
            expires_at_ms: Set(expires_at_ms),
        };
        let mut conflict =
            OnConflict::columns([fill_lease::Column::Endpoint, fill_lease::Column::LeaseKey]);
        conflict.do_nothing();
        match fill_lease::Entity::insert(active)
            .on_conflict(conflict.to_owned())
            .do_nothing()
            .exec(&self.db)
            .await?
        {
            TryInsertResult::Inserted(_) => Ok(true),
            TryInsertResult::Conflicted | TryInsertResult::Empty => Ok(false),
        }
    }

    async fn release_fill_lease(&self, key: &FillLeaseKey, owner: &str) -> Result<()> {
        fill_lease::Entity::delete_many()
            .filter(
                Condition::all()
                    .add(fill_lease::Column::Endpoint.eq(key.endpoint.as_str()))
                    .add(fill_lease::Column::LeaseKey.eq(key.key.clone()))
                    .add(fill_lease::Column::Owner.eq(owner.to_string())),
            )
            .exec(&self.db)
            .await?;
        Ok(())
    }
}

fn replay_upsert_conflict() -> OnConflict {
    let mut conflict = OnConflict::columns([
        replay_record::Column::CaptureTs,
        replay_record::Column::Modifier,
        replay_record::Column::CanonicalOriginalUrl,
    ]);
    conflict.update_columns([
        replay_record::Column::Status,
        replay_record::Column::Headers,
        replay_record::Column::BlobKey,
        replay_record::Column::Sha256,
        replay_record::Column::BodySize,
        replay_record::Column::Classification,
        replay_record::Column::ObservedSize,
    ]);
    conflict.to_owned()
}

fn metadata_upsert_conflict() -> OnConflict {
    let mut conflict = OnConflict::columns([
        metadata_record::Column::Endpoint,
        metadata_record::Column::NormalizedQuery,
    ]);
    conflict.update_columns([
        metadata_record::Column::Status,
        metadata_record::Column::Headers,
        metadata_record::Column::Body,
        metadata_record::Column::Sha256,
        metadata_record::Column::BodySize,
        metadata_record::Column::Classification,
        metadata_record::Column::ObservedSize,
    ]);
    conflict.to_owned()
}

fn fill_due_condition(now_ms: i64) -> Condition {
    Condition::all()
        .add(fill_queue::Column::NextAttemptAtMs.lte(now_ms))
        .add(
            Condition::any()
                .add(fill_queue::Column::LeaseOwner.is_null())
                .add(fill_queue::Column::LeaseExpiresAtMs.lte(now_ms)),
        )
}

fn fill_row_condition(endpoint: &str, fill_key: &str) -> Condition {
    Condition::all()
        .add(fill_queue::Column::Endpoint.eq(endpoint.to_string()))
        .add(fill_queue::Column::FillKey.eq(fill_key.to_string()))
}

fn metadata_endpoint_from_str(endpoint: &str) -> Option<Endpoint> {
    Endpoint::from_str(endpoint).filter(|endpoint| *endpoint != Endpoint::Replay)
}

fn epoch_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock must be after unix epoch")
        .as_millis()
        .try_into()
        .unwrap_or(i64::MAX)
}

fn duration_ms(duration: Duration) -> i64 {
    duration.as_millis().try_into().unwrap_or(i64::MAX)
}

fn stored_status(status: i32) -> Result<StatusCode> {
    let status: u16 = status
        .try_into()
        .map_err(|_| anyhow!("stored status out of range: {status}"))?;
    StatusCode::from_u16(status).map_err(|error| anyhow!("stored status invalid: {error}"))
}

fn headers_to_json(headers: &HeaderMap) -> Result<Value> {
    Ok(Value::Array(
        headers
            .iter()
            .filter_map(|(name, value)| {
                value
                    .to_str()
                    .ok()
                    .map(|value| Value::Array(vec![name.as_str().into(), value.into()]))
            })
            .collect(),
    ))
}

fn headers_from_json(value: Value) -> Result<HeaderMap> {
    let pairs = serde_json::from_value::<Vec<(String, String)>>(value)?;
    let mut headers = HeaderMap::new();
    for (name, value) in pairs {
        headers.insert(
            HeaderName::from_bytes(name.as_bytes())?,
            HeaderValue::from_str(&value)?,
        );
    }
    Ok(headers)
}
