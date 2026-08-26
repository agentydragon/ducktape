//! Encodes the importer-canary requirements
//! (`cluster/docs/activitywatch/importer-canary.md`): a second import of the same
//! frozen inputs adds zero events, the source bytes are unchanged, distinct
//! `localhost` devices never merge, and conflict/unexpected inbox files fail
//! closed.

use std::fs;
use std::path::Path;
use std::path::PathBuf;

use aw_datastore::Datastore;
use aw_importer::ImportError;
use aw_importer::import_inbox;
use chrono::DateTime;
use chrono::Utc;
use rusqlite::Connection;
use rusqlite::params;
use serde_json::json;

struct BucketSpec {
    name: String,
    type_: String,
    client: String,
    hostname: String,
    events: Vec<(i64, i64, String)>,
}

fn bucket(
    name: &str,
    type_: &str,
    client: &str,
    hostname: &str,
    events: &[(i64, i64, &str)],
) -> BucketSpec {
    BucketSpec {
        name: name.into(),
        type_: type_.into(),
        client: client.into(),
        hostname: hostname.into(),
        events: events
            .iter()
            .map(|(start, end, data)| (*start, *end, (*data).to_string()))
            .collect(),
    }
}

/// Write a frozen aw-server-rust snapshot (schema v5) as a single clean SQLite
/// file: rollback-journal mode, so no `-wal`/`-shm` remains beside it.
fn write_snapshot(path: &Path, buckets: &[BucketSpec]) {
    let conn = Connection::open(path).expect("open fixture");
    conn.execute_batch(
        "PRAGMA journal_mode=DELETE;
         PRAGMA user_version=5;
         CREATE TABLE buckets (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             name TEXT UNIQUE NOT NULL,
             type TEXT NOT NULL,
             client TEXT NOT NULL,
             hostname TEXT NOT NULL,
             created TEXT NOT NULL,
             data TEXT NOT NULL DEFAULT '{}'
         );
         CREATE TABLE events (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             bucketrow INTEGER NOT NULL,
             starttime INTEGER NOT NULL,
             endtime INTEGER NOT NULL,
             data TEXT NOT NULL,
             FOREIGN KEY (bucketrow) REFERENCES buckets(id)
         );",
    )
    .expect("create schema");
    let created: DateTime<Utc> = DateTime::from_timestamp(1_600_000_000, 0).unwrap();
    for spec in buckets {
        conn.execute(
            "INSERT INTO buckets (name, type, client, hostname, created, data)
             VALUES (?1, ?2, ?3, ?4, ?5, '{}')",
            params![spec.name, spec.type_, spec.client, spec.hostname, created],
        )
        .expect("insert bucket");
        let bid = conn.last_insert_rowid();
        for (start, end, data) in &spec.events {
            conn.execute(
                "INSERT INTO events (bucketrow, starttime, endtime, data)
                 VALUES (?1, ?2, ?3, ?4)",
                params![bid, start, end, data],
            )
            .expect("insert event");
        }
    }
}

fn temp_root(tag: &str) -> PathBuf {
    let base = std::env::var("TEST_TMPDIR")
        .unwrap_or_else(|_| std::env::temp_dir().to_string_lossy().into_owned());
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let dir = PathBuf::from(base).join(format!("aw-importer-{tag}-{}-{nanos}", std::process::id()));
    fs::create_dir_all(&dir).unwrap();
    dir
}

fn central(root: &Path) -> Datastore {
    Datastore::new(
        root.join("central.sqlite").to_string_lossy().into_owned(),
        false,
    )
}

fn events(datastore: &Datastore, bucket_id: &str) -> Vec<aw_models::Event> {
    datastore
        .get_events_unclipped(bucket_id, None, None, None)
        .unwrap()
}

#[test]
fn reimport_adds_nothing_and_leaves_source_bytes_unchanged() {
    let root = temp_root("idem");
    let device_dir = root.join("inbox").join("rugged");
    fs::create_dir_all(&device_dir).unwrap();
    let snapshot = device_dir.join("aw.db");

    // AFK: repeated heartbeat rows for the same tuple (amplification); the store
    // must keep only the distinct tuples.
    let afk = bucket(
        "aw-watcher-afk_localhost",
        "afkstatus",
        "aw-watcher-afk",
        "localhost",
        &[
            (
                1_000_000_000_000,
                2_000_000_000_000,
                r#"{"status":"not-afk"}"#,
            ),
            (
                1_000_000_000_000,
                2_000_000_000_000,
                r#"{"status":"not-afk"}"#,
            ),
            (
                1_000_000_000_000,
                2_000_000_000_000,
                r#"{"status":"not-afk"}"#,
            ),
            (2_000_000_000_000, 3_000_000_000_000, r#"{"status":"afk"}"#),
        ],
    );
    // Window: two rows with the same (start,end) whose data differs only in key
    // order — canonicalization must treat them as one tuple.
    let window = bucket(
        "aw-watcher-window_localhost",
        "currentwindow",
        "aw-watcher-window",
        "localhost",
        &[
            (
                1_000_000_000_000,
                1_500_000_000_000,
                r#"{"app":"code","title":"a"}"#,
            ),
            (
                1_000_000_000_000,
                1_500_000_000_000,
                r#"{"title":"a","app":"code"}"#,
            ),
            (
                1_500_000_000_000,
                2_000_000_000_000,
                r#"{"app":"firefox","title":"b"}"#,
            ),
        ],
    );
    write_snapshot(&snapshot, &[afk, window]);
    let before = fs::read(&snapshot).unwrap();

    let inbox = root.join("inbox");
    let datastore = central(&root);

    let summary = import_inbox(&inbox, &datastore).expect("first import");
    assert_eq!(summary.total_inserted(), 4);
    assert_eq!(
        events(&datastore, "rugged::aw-watcher-afk_localhost").len(),
        2
    );
    assert_eq!(
        events(&datastore, "rugged::aw-watcher-window_localhost").len(),
        2
    );

    let second = import_inbox(&inbox, &datastore).expect("second import");
    assert_eq!(second.total_inserted(), 0, "re-import must add nothing");
    assert_eq!(
        events(&datastore, "rugged::aw-watcher-afk_localhost").len(),
        2
    );
    assert_eq!(
        events(&datastore, "rugged::aw-watcher-window_localhost").len(),
        2
    );

    datastore.close();

    let after = fs::read(&snapshot).unwrap();
    assert_eq!(before, after, "source snapshot bytes changed");
    let mut side: Vec<String> = fs::read_dir(&device_dir)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    side.sort();
    assert_eq!(
        side,
        vec!["aw.db".to_string()],
        "unexpected side files: {side:?}"
    );
}

#[test]
fn localhost_buckets_from_different_devices_stay_separate() {
    let root = temp_root("localhost");
    let inbox = root.join("inbox");

    let rugged = inbox.join("rugged");
    fs::create_dir_all(&rugged).unwrap();
    write_snapshot(
        &rugged.join("aw.db"),
        &[bucket(
            "aw-watcher-web-chrome_localhost",
            "web.tab.current",
            "aw-watcher-web",
            "localhost",
            &[(
                1_000_000_000_000,
                2_000_000_000_000,
                r#"{"url":"https://rugged.example"}"#,
            )],
        )],
    );

    let wyrm2 = inbox.join("wyrm2");
    fs::create_dir_all(&wyrm2).unwrap();
    write_snapshot(
        &wyrm2.join("aw.db"),
        &[bucket(
            "aw-watcher-web-chrome_localhost",
            "web.tab.current",
            "aw-watcher-web",
            "localhost",
            &[(
                1_000_000_000_000,
                2_000_000_000_000,
                r#"{"url":"https://wyrm2.example"}"#,
            )],
        )],
    );

    let datastore = central(&root);
    let summary = import_inbox(&inbox, &datastore).expect("import");
    assert_eq!(summary.total_inserted(), 2);

    let buckets = datastore.get_buckets().unwrap();
    let rugged_id = "rugged::aw-watcher-web-chrome_localhost";
    let wyrm2_id = "wyrm2::aw-watcher-web-chrome_localhost";
    assert!(buckets.contains_key(rugged_id));
    assert!(buckets.contains_key(wyrm2_id));
    // Provenance is the device directory, not the source `localhost` hostname.
    assert_eq!(buckets[rugged_id].hostname, "rugged");
    assert_eq!(buckets[wyrm2_id].hostname, "wyrm2");

    let rugged_events = events(&datastore, rugged_id);
    assert_eq!(rugged_events.len(), 1);
    assert_eq!(
        rugged_events[0].data.get("url").unwrap(),
        &json!("https://rugged.example")
    );
    let wyrm2_events = events(&datastore, wyrm2_id);
    assert_eq!(wyrm2_events.len(), 1);
    assert_eq!(
        wyrm2_events[0].data.get("url").unwrap(),
        &json!("https://wyrm2.example")
    );

    datastore.close();
}

#[test]
fn conflict_file_fails_closed() {
    let root = temp_root("conflict");
    let device_dir = root.join("inbox").join("rugged");
    fs::create_dir_all(&device_dir).unwrap();
    let afk = || {
        bucket(
            "aw-watcher-afk_localhost",
            "afkstatus",
            "aw-watcher-afk",
            "localhost",
            &[(1_000_000_000_000, 2_000_000_000_000, r#"{"status":"afk"}"#)],
        )
    };
    write_snapshot(&device_dir.join("aw.db"), &[afk()]);
    write_snapshot(
        &device_dir.join("aw.sync-conflict-20260721-035404-PATWINW.db"),
        &[afk()],
    );

    let datastore = central(&root);
    let error = import_inbox(&root.join("inbox"), &datastore).expect_err("must fail closed");
    assert!(
        matches!(error, ImportError::ConflictFile(_)),
        "got {error:?}"
    );
    assert!(
        datastore.get_buckets().unwrap().is_empty(),
        "nothing must be imported"
    );
    datastore.close();
}

#[test]
fn ambiguous_snapshot_fails_closed() {
    let root = temp_root("ambiguous");
    let device_dir = root.join("inbox").join("rugged");
    fs::create_dir_all(&device_dir).unwrap();
    let afk = || {
        bucket(
            "aw-watcher-afk_localhost",
            "afkstatus",
            "aw-watcher-afk",
            "localhost",
            &[(1_000_000_000_000, 2_000_000_000_000, r#"{"status":"afk"}"#)],
        )
    };
    write_snapshot(&device_dir.join("aw.db"), &[afk()]);
    write_snapshot(&device_dir.join("aw-2.db"), &[afk()]);

    let datastore = central(&root);
    let error = import_inbox(&root.join("inbox"), &datastore).expect_err("must fail closed");
    assert!(
        matches!(error, ImportError::AmbiguousSnapshot { .. }),
        "got {error:?}"
    );
    assert!(
        datastore.get_buckets().unwrap().is_empty(),
        "nothing must be imported"
    );
    datastore.close();
}

#[test]
fn unexpected_file_fails_closed() {
    let root = temp_root("unexpected");
    let device_dir = root.join("inbox").join("rugged");
    fs::create_dir_all(&device_dir).unwrap();
    write_snapshot(
        &device_dir.join("aw.db"),
        &[bucket(
            "aw-watcher-afk_localhost",
            "afkstatus",
            "aw-watcher-afk",
            "localhost",
            &[(1_000_000_000_000, 2_000_000_000_000, r#"{"status":"afk"}"#)],
        )],
    );
    fs::write(device_dir.join("notes.txt"), b"stray file").unwrap();

    let datastore = central(&root);
    let error = import_inbox(&root.join("inbox"), &datastore).expect_err("must fail closed");
    assert!(
        matches!(error, ImportError::UnexpectedEntry(_)),
        "got {error:?}"
    );
    assert!(
        datastore.get_buckets().unwrap().is_empty(),
        "nothing must be imported"
    );
    datastore.close();
}
