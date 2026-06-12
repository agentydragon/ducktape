use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Result, anyhow};
use async_trait::async_trait;
use bytes::Bytes;
use sea_orm::entity::prelude::*;
use sea_orm::sea_query::{Expr, OnConflict};
use sea_orm::{
    ColumnTrait, Condition, ConnectionTrait, Database, DatabaseConnection, DbBackend, EntityTrait,
    QueryFilter, Schema, Set, TryInsertResult,
};
use serde_json::Value;

use crate::store::{ArchiveStore, BlobStore};
use crate::types::{
    Endpoint, FillLeaseKey, MetadataKey, MetadataRecord, ReplayKey, ReplayRecord, StoredMetadata,
    StoredReplay,
};

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

pub struct PostgresArchiveStore {
    db: DatabaseConnection,
    blobs: Arc<dyn BlobStore>,
}

impl PostgresArchiveStore {
    pub async fn new(database_url: String, blobs: Arc<dyn BlobStore>) -> Result<Self> {
        let db = Database::connect(database_url).await?;
        let store = Self { db, blobs };
        store.migrate().await?;
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
            status: row
                .status
                .ok_or_else(|| anyhow!("replay row missing status for {key}"))?
                .try_into()?,
            headers: serde_json::from_value(row.headers)?,
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
                let classification = if record.status >= 400 {
                    "archived_error"
                } else {
                    "served_capture"
                };
                let active = replay_record::ActiveModel {
                    capture_ts: Set(record.key.capture_ts),
                    modifier: Set(record.key.modifier),
                    canonical_original_url: Set(record.key.original_url),
                    status: Set(Some(record.status as i32)),
                    headers: Set(serde_json::to_value(&record.headers)?),
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
            status: row
                .status
                .ok_or_else(|| anyhow!("metadata row missing status"))?
                .try_into()?,
            headers: serde_json::from_value(row.headers)?,
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
                    status: Set(Some(record.status as i32)),
                    headers: Set(serde_json::to_value(&record.headers)?),
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

fn metadata_endpoint_from_str(endpoint: &str) -> Option<Endpoint> {
    match endpoint {
        "availability" => Some(Endpoint::Availability),
        "cdx" => Some(Endpoint::Cdx),
        _ => None,
    }
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
