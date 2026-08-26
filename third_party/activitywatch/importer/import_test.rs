//! The importer's behavioral contract
//! (`cluster/docs/activitywatch/importer-canary.md`): a second import of the same
//! frozen inputs adds zero events, the source bytes are never touched, and distinct
//! devices whose watchers both report hostname `localhost` never merge.

use std::fs;
use std::path::Path;
use std::path::PathBuf;

use aw_datastore::Datastore;
use aw_importer::import_snapshots;
use aw_models::Bucket;
use aw_models::BucketMetadata;
use chrono::DateTime;
use chrono::Utc;
use rusqlite::Connection;
use rusqlite::params;
use serde_json::Map;
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

/// Write a frozen aw-server-rust snapshot. aw's own datastore lays down the real,
/// fully-migrated schema and the bucket rows, so this fixture can't drift from the
/// schema the importer actually reads — real v5 also carries `buckets.data_deprecated`,
/// a `key_value` table and a composite index that a hand-copied `CREATE TABLE` misses.
/// Event rows are then inserted directly, on purpose: they carry amplified duplicate
/// heartbeats that aw's own `insert_events` would coalesce, which is exactly what the
/// importer must dedup. aw uses SQLite's default rollback journal, so the store closes
/// to a single file with no `-wal`/`-shm` beside it.
fn write_snapshot(path: &Path, buckets: &[BucketSpec]) {
    let created: DateTime<Utc> = DateTime::from_timestamp(1_600_000_000, 0).unwrap();
    {
        let datastore = Datastore::new(path.to_string_lossy().into_owned(), false);
        for spec in buckets {
            datastore
                .create_bucket(&Bucket {
                    bid: None,
                    id: spec.name.clone(),
                    _type: spec.type_.clone(),
                    client: spec.client.clone(),
                    hostname: spec.hostname.clone(),
                    created: Some(created),
                    data: Map::new(),
                    metadata: BucketMetadata::default(),
                    events: None,
                    last_updated: None,
                })
                .expect("create bucket");
        }
        datastore.close();
    }
    let conn = Connection::open(path).expect("reopen fixture");
    for spec in buckets {
        let bid: i64 = conn
            .query_row(
                "SELECT id FROM buckets WHERE name = ?1",
                params![spec.name],
                |row| row.get(0),
            )
            .expect("bucket row id");
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

    let datastore = central(&root);

    let summary =
        import_snapshots(std::slice::from_ref(&snapshot), &datastore).expect("first import");
    assert_eq!(summary.total_inserted(), 4);
    assert_eq!(
        events(&datastore, "rugged::aw-watcher-afk_localhost").len(),
        2
    );
    assert_eq!(
        events(&datastore, "rugged::aw-watcher-window_localhost").len(),
        2
    );

    let second =
        import_snapshots(std::slice::from_ref(&snapshot), &datastore).expect("second import");
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
    let summary =
        import_snapshots(&[rugged.join("aw.db"), wyrm2.join("aw.db")], &datastore).expect("import");
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
fn dedup_reads_only_the_source_time_window_yet_still_finds_collisions() {
    // A bucket accumulates events far apart in time across imports. The dedup read
    // must be scoped to each source batch's own time window (not the whole bucket),
    // yet still catch a collision inside that window.
    let root = temp_root("window");
    let device_dir = root.join("inbox").join("rugged");
    fs::create_dir_all(&device_dir).unwrap();
    let snapshot = device_dir.join("aw.db");
    let datastore = central(&root);
    let afk = |start: i64, end: i64, status: &str| {
        bucket(
            "aw-watcher-afk_localhost",
            "afkstatus",
            "aw-watcher-afk",
            "localhost",
            &[(start, end, status)],
        )
    };

    write_snapshot(
        &snapshot,
        &[afk(
            1_000_000_000_000,
            2_000_000_000_000,
            r#"{"status":"afk"}"#,
        )],
    );
    assert_eq!(
        import_snapshots(std::slice::from_ref(&snapshot), &datastore)
            .expect("import 1")
            .total_inserted(),
        1
    );

    // A much later event in the same bucket. Its window sits far past the stored
    // early event, so the dedup read finds nothing to compare against — proving the
    // read is scoped to the window, not the whole bucket.
    fs::remove_file(&snapshot).unwrap();
    write_snapshot(
        &snapshot,
        &[afk(
            9_000_000_000_000,
            9_500_000_000_000,
            r#"{"status":"not-afk"}"#,
        )],
    );
    let second = import_snapshots(std::slice::from_ref(&snapshot), &datastore).expect("import 2");
    assert_eq!(second.total_inserted(), 1);
    assert_eq!(
        second.buckets[0].existing_in_window, 0,
        "read was not windowed"
    );

    // Re-import the early batch: its window is around t=1e12, far from the stored
    // 9e12 event, yet the 1e12 collision must still be found → nothing inserted.
    fs::remove_file(&snapshot).unwrap();
    write_snapshot(
        &snapshot,
        &[afk(
            1_000_000_000_000,
            2_000_000_000_000,
            r#"{"status":"afk"}"#,
        )],
    );
    assert_eq!(
        import_snapshots(std::slice::from_ref(&snapshot), &datastore)
            .expect("reimport 1")
            .total_inserted(),
        0,
        "windowed dedup missed the stored collision"
    );

    assert_eq!(
        events(&datastore, "rugged::aw-watcher-afk_localhost").len(),
        2
    );
    datastore.close();
}
