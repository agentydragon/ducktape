//! Read-only access to an ActivityWatch source snapshot (aw-server-rust SQLite
//! schema, `db_version >= 2`). Opened `immutable=1` so SQLite performs no
//! migration and creates no `-wal`/`-shm`/`-journal` side files.

use std::path::Path;

use chrono::DateTime;
use chrono::Utc;
use rusqlite::Connection;
use rusqlite::OpenFlags;
use serde_json::Map;
use serde_json::Value;

use crate::ImportError;

pub(crate) struct SourceBucket {
    pub bid: i64,
    pub name: String,
    pub type_: String,
    pub client: String,
    pub created: Option<DateTime<Utc>>,
    pub data: Map<String, Value>,
}

pub(crate) struct SourceEvent {
    pub start_nanos: i64,
    pub end_nanos: i64,
    pub data: String,
}

pub(crate) fn open_readonly(path: &Path) -> Result<Connection, ImportError> {
    let absolute = std::fs::canonicalize(path).map_err(|source| ImportError::Io {
        path: path.to_path_buf(),
        source,
    })?;
    Connection::open_with_flags(
        file_uri(&absolute),
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
    )
    .map_err(|source| ImportError::Sqlite {
        path: path.to_path_buf(),
        source,
    })
}

pub(crate) fn read_buckets(
    conn: &Connection,
    path: &Path,
) -> Result<Vec<SourceBucket>, ImportError> {
    let rows = read_bucket_rows(conn).map_err(|source| ImportError::Sqlite {
        path: path.to_path_buf(),
        source,
    })?;
    rows.into_iter()
        .map(|(bid, name, type_, client, created, data_str)| {
            let data = serde_json::from_str(&data_str).map_err(|source| {
                ImportError::CorruptBucketData {
                    path: path.to_path_buf(),
                    source,
                }
            })?;
            Ok(SourceBucket {
                bid,
                name,
                type_,
                client,
                created,
                data,
            })
        })
        .collect()
}

pub(crate) fn read_events(
    conn: &Connection,
    path: &Path,
    bucketrow: i64,
) -> Result<Vec<SourceEvent>, ImportError> {
    read_event_rows(conn, bucketrow).map_err(|source| ImportError::Sqlite {
        path: path.to_path_buf(),
        source,
    })
}

type BucketRow = (i64, String, String, String, Option<DateTime<Utc>>, String);

fn read_bucket_rows(conn: &Connection) -> Result<Vec<BucketRow>, rusqlite::Error> {
    let mut stmt = conn.prepare("SELECT id, name, type, client, created, data FROM buckets")?;
    let rows = stmt.query_map([], |row| {
        Ok((
            row.get(0)?,
            row.get(1)?,
            row.get(2)?,
            row.get(3)?,
            row.get(4)?,
            row.get(5)?,
        ))
    })?;
    rows.collect()
}

fn read_event_rows(conn: &Connection, bucketrow: i64) -> Result<Vec<SourceEvent>, rusqlite::Error> {
    let mut stmt =
        conn.prepare("SELECT starttime, endtime, data FROM events WHERE bucketrow = ?1")?;
    let rows = stmt.query_map([bucketrow], |row| {
        Ok(SourceEvent {
            start_nanos: row.get(0)?,
            end_nanos: row.get(1)?,
            data: row.get(2)?,
        })
    })?;
    rows.collect()
}

/// Build a `file:` URI so the `immutable=1` query parameter reaches SQLite.
/// Only the characters significant to URI parsing are percent-encoded; on Linux
/// the canonical absolute path is otherwise passed through as UTF-8.
fn file_uri(path: &Path) -> String {
    let mut uri = String::from("file:");
    for ch in path.to_string_lossy().chars() {
        match ch {
            '?' => uri.push_str("%3F"),
            '#' => uri.push_str("%23"),
            '%' => uri.push_str("%25"),
            other => uri.push(other),
        }
    }
    uri.push_str("?immutable=1");
    uri
}
