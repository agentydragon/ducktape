//! The importer's behavioral contract, exercised against two real aw-servers: it
//! reads a source aw-server and folds its data into a destination aw-server, a
//! second import of unchanged source data adds nothing, distinct devices whose
//! watchers both report hostname `localhost` never merge, and the source server is
//! only ever read. This is the exact topology production runs (a desktop aw-server
//! synced into the central one), so the REST read/insert semantics are covered for
//! real, not modelled.

use std::fs;
use std::net::TcpListener;
use std::path::Path;
use std::path::PathBuf;
use std::process::Child;
use std::process::Command;
use std::process::Stdio;
use std::sync::Once;
use std::time::Duration;

use aw_client_rust::AwClient;
use aw_importer::ImportOptions;
use aw_importer::connect;
use aw_importer::import_device_with_options;
use aw_models::Bucket;
use aw_models::BucketMetadata;
use aw_models::Event;
use chrono::DateTime;
use chrono::TimeDelta;
use runfiles::Runfiles;
use serde_json::Map;
use serde_json::Value;
use serde_json::json;

/// `AwClient::new` takes a `SingleInstance` lock under the client's XDG cache dir;
/// point HOME/XDG_CACHE_HOME at a writable dir so it can't land on an unwritable
/// sandbox path. Done once (tests run single-threaded via `RUST_TEST_THREADS=1`).
fn client_env_setup() {
    static ONCE: Once = Once::new();
    ONCE.call_once(|| {
        let base = std::env::var("TEST_TMPDIR")
            .unwrap_or_else(|_| std::env::temp_dir().to_string_lossy().into_owned());
        let cache = PathBuf::from(&base).join("client-xdg-cache");
        fs::create_dir_all(&cache).unwrap();
        // SAFETY: single-threaded test process (RUST_TEST_THREADS=1), no concurrent env access.
        unsafe {
            std::env::set_var("HOME", &base);
            std::env::set_var("XDG_CACHE_HOME", &cache);
        }
    });
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

fn aw_server_bin() -> PathBuf {
    let runfiles = Runfiles::create().expect("runfiles");
    let rel = std::env::var("AW_SERVER_BIN").expect("AW_SERVER_BIN env set by the BUILD rule");
    runfiles
        .rlocation_from(&rel, "")
        .unwrap_or_else(|| panic!("aw_server_bin not in runfiles at {rel}"))
}

fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

/// A running aw-server child, killed when the guard drops.
struct ServerGuard {
    child: Child,
}

impl Drop for ServerGuard {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn spawn_server(datadir: &Path, port: u16, api_key: Option<&str>) -> ServerGuard {
    fs::create_dir_all(datadir).unwrap();
    let mut command = Command::new(aw_server_bin());
    command
        .arg("--testing")
        .arg("--host")
        .arg("127.0.0.1")
        .arg("--port")
        .arg(port.to_string())
        .arg("--dbpath")
        .arg(datadir.join("aw.sqlite"))
        .env("HOME", datadir)
        .env("XDG_DATA_HOME", datadir.join("data"))
        .env("XDG_CONFIG_HOME", datadir.join("config"))
        .env("XDG_CACHE_HOME", datadir.join("cache"));
    // The api_key is config-file-only (no flag/env in aw-server), so when the test
    // wants a gated server it writes a minimal config and points --config at it.
    if let Some(key) = api_key {
        let config = datadir.join("aw-server.toml");
        fs::write(&config, format!("[auth]\napi_key = \"{key}\"\n")).unwrap();
        command.arg("--config").arg(config);
    }
    let child = command
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn aw-server");
    ServerGuard { child }
}

async fn wait_ready(client: &AwClient) {
    for _ in 0..300 {
        if client.get_buckets().await.is_ok() {
            return;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    panic!("aw-server did not become ready in time");
}

/// Start a fresh aw-server under `root/name` and return it plus a client. Keep the
/// guard in scope for the test's lifetime; dropping it stops the server. `name`
/// also names the client's single-instance lock, so every server in one test needs
/// a distinct name.
async fn aw_server(root: &Path, name: &str) -> (ServerGuard, AwClient) {
    aw_server_authed(root, name, None).await
}

/// Like `aw_server`, but the server requires `api_key` on every `/api/*` call
/// (except `/api/0/info`) and the returned client carries it as a bearer.
async fn aw_server_authed(
    root: &Path,
    name: &str,
    api_key: Option<&str>,
) -> (ServerGuard, AwClient) {
    client_env_setup();
    let port = free_port();
    let guard = spawn_server(&root.join(name), port, api_key);
    let url = format!("http://127.0.0.1:{port}");
    let client = connect(&url, api_key.map(str::to_string), name).expect("client");
    wait_ready(&client).await;
    (guard, client)
}

fn event(start_nanos: i64, end_nanos: i64, data: &str) -> Event {
    let data: Map<String, Value> = serde_json::from_str(data).expect("event data json");
    Event::new(
        DateTime::from_timestamp_nanos(start_nanos),
        TimeDelta::nanoseconds(end_nanos - start_nanos),
        data,
    )
}

/// Create a bucket on `client` and insert `events` into it. `insert_events` is a
/// plain insert (only `heartbeat` coalesces), so duplicate rows are stored as-is --
/// which is exactly the amplified state the importer must dedup.
async fn seed_bucket(
    client: &AwClient,
    id: &str,
    type_: &str,
    watcher: &str,
    hostname: &str,
    events: Vec<Event>,
) {
    let created = DateTime::from_timestamp(1_600_000_000, 0).unwrap();
    client
        .create_bucket(&Bucket {
            bid: None,
            id: id.to_string(),
            _type: type_.to_string(),
            client: watcher.to_string(),
            hostname: hostname.to_string(),
            created: Some(created),
            data: Map::new(),
            metadata: BucketMetadata::default(),
            events: None,
            last_updated: None,
        })
        .await
        .expect("create source bucket");
    if !events.is_empty() {
        client.insert_events(id, events).await.expect("seed events");
    }
}

async fn events(client: &AwClient, bucket_id: &str) -> Vec<Event> {
    client
        .get_events(bucket_id, None, None, None)
        .await
        .unwrap()
}

async fn import_device(
    source: &AwClient,
    dest: &AwClient,
    device: &str,
) -> Result<aw_importer::ImportSummary, aw_importer::ImportError> {
    import_device_with_options(source, dest, device, ImportOptions::default()).await
}

async fn import_device_with_options_for_test(
    source: &AwClient,
    dest: &AwClient,
    device: &str,
    options: ImportOptions,
) -> Result<aw_importer::ImportSummary, aw_importer::ImportError> {
    import_device_with_options(source, dest, device, options).await
}

fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
}

#[test]
fn reimport_adds_nothing_and_leaves_source_untouched() {
    runtime().block_on(async {
        let root = temp_root("idem");
        let (_source_server, source) = aw_server(&root, "source").await;
        let (_dest_server, dest) = aw_server(&root, "dest").await;

        // AFK: repeated heartbeat rows for the same tuple (amplification); the store
        // must keep only the distinct tuples.
        seed_bucket(
            &source,
            "aw-watcher-afk_localhost",
            "afkstatus",
            "aw-watcher-afk",
            "localhost",
            vec![
                event(
                    1_000_000_000_000,
                    2_000_000_000_000,
                    r#"{"status":"not-afk"}"#,
                ),
                event(
                    1_000_000_000_000,
                    2_000_000_000_000,
                    r#"{"status":"not-afk"}"#,
                ),
                event(
                    1_000_000_000_000,
                    2_000_000_000_000,
                    r#"{"status":"not-afk"}"#,
                ),
                event(2_000_000_000_000, 3_000_000_000_000, r#"{"status":"afk"}"#),
            ],
        )
        .await;
        // Window: two rows with the same (start,end) whose data differs only in key
        // order -- the servers store both, sorted-key, so they read back identical
        // and collapse to one tuple.
        seed_bucket(
            &source,
            "aw-watcher-window_localhost",
            "currentwindow",
            "aw-watcher-window",
            "localhost",
            vec![
                event(
                    1_000_000_000_000,
                    1_500_000_000_000,
                    r#"{"app":"code","title":"a"}"#,
                ),
                event(
                    1_000_000_000_000,
                    1_500_000_000_000,
                    r#"{"title":"a","app":"code"}"#,
                ),
                event(
                    1_500_000_000_000,
                    2_000_000_000_000,
                    r#"{"app":"firefox","title":"b"}"#,
                ),
            ],
        )
        .await;

        let afk_before = source
            .get_event_count("aw-watcher-afk_localhost")
            .await
            .unwrap();
        let window_before = source
            .get_event_count("aw-watcher-window_localhost")
            .await
            .unwrap();
        assert_eq!(afk_before, 4);
        assert_eq!(window_before, 3);

        let summary = import_device(&source, &dest, "rugged")
            .await
            .expect("first import");
        assert_eq!(summary.total_inserted(), 4);
        assert_eq!(
            events(&dest, "rugged::aw-watcher-afk_localhost")
                .await
                .len(),
            2
        );
        assert_eq!(
            events(&dest, "rugged::aw-watcher-window_localhost")
                .await
                .len(),
            2
        );

        let second = import_device(&source, &dest, "rugged")
            .await
            .expect("second import");
        assert_eq!(second.total_inserted(), 0, "re-import must add nothing");
        assert_eq!(
            events(&dest, "rugged::aw-watcher-afk_localhost")
                .await
                .len(),
            2
        );
        assert_eq!(
            events(&dest, "rugged::aw-watcher-window_localhost")
                .await
                .len(),
            2
        );

        // Read-only source: the importer only ever GETs from the source server.
        assert_eq!(
            source
                .get_event_count("aw-watcher-afk_localhost")
                .await
                .unwrap(),
            afk_before
        );
        assert_eq!(
            source
                .get_event_count("aw-watcher-window_localhost")
                .await
                .unwrap(),
            window_before
        );
    });
}

#[test]
fn localhost_buckets_from_different_devices_stay_separate() {
    runtime().block_on(async {
        let root = temp_root("localhost");
        let (_rugged_server, rugged) = aw_server(&root, "rugged-src").await;
        let (_wyrm2_server, wyrm2) = aw_server(&root, "wyrm2-src").await;
        let (_dest_server, dest) = aw_server(&root, "dest").await;

        seed_bucket(
            &rugged,
            "aw-watcher-web-chrome_localhost",
            "web.tab.current",
            "aw-watcher-web",
            "localhost",
            vec![event(
                1_000_000_000_000,
                2_000_000_000_000,
                r#"{"url":"https://rugged.example"}"#,
            )],
        )
        .await;
        seed_bucket(
            &wyrm2,
            "aw-watcher-web-chrome_localhost",
            "web.tab.current",
            "aw-watcher-web",
            "localhost",
            vec![event(
                1_000_000_000_000,
                2_000_000_000_000,
                r#"{"url":"https://wyrm2.example"}"#,
            )],
        )
        .await;

        assert_eq!(
            import_device(&rugged, &dest, "rugged")
                .await
                .unwrap()
                .total_inserted(),
            1
        );
        assert_eq!(
            import_device(&wyrm2, &dest, "wyrm2")
                .await
                .unwrap()
                .total_inserted(),
            1
        );

        let buckets = dest.get_buckets().await.unwrap();
        let rugged_id = "rugged::aw-watcher-web-chrome_localhost";
        let wyrm2_id = "wyrm2::aw-watcher-web-chrome_localhost";
        assert!(buckets.contains_key(rugged_id));
        assert!(buckets.contains_key(wyrm2_id));
        // Provenance is the device id, not the source `localhost` hostname.
        assert_eq!(buckets[rugged_id].hostname, "rugged");
        assert_eq!(buckets[wyrm2_id].hostname, "wyrm2");

        let rugged_events = events(&dest, rugged_id).await;
        assert_eq!(rugged_events.len(), 1);
        assert_eq!(
            rugged_events[0].data.get("url").unwrap(),
            &json!("https://rugged.example")
        );
        let wyrm2_events = events(&dest, wyrm2_id).await;
        assert_eq!(wyrm2_events.len(), 1);
        assert_eq!(
            wyrm2_events[0].data.get("url").unwrap(),
            &json!("https://wyrm2.example")
        );
    });
}

#[test]
fn growing_source_imports_only_the_delta() {
    // The common production case: the desktop accumulates events between syncs, and
    // each import inserts only what is new while the already-synced events dedup.
    runtime().block_on(async {
        let root = temp_root("growth");
        let (_source_server, source) = aw_server(&root, "source").await;
        let (_dest_server, dest) = aw_server(&root, "dest").await;

        seed_bucket(
            &source,
            "aw-watcher-afk_localhost",
            "afkstatus",
            "aw-watcher-afk",
            "localhost",
            vec![event(
                1_000_000_000_000,
                2_000_000_000_000,
                r#"{"status":"afk"}"#,
            )],
        )
        .await;
        assert_eq!(
            import_device(&source, &dest, "rugged")
                .await
                .unwrap()
                .total_inserted(),
            1
        );

        // The source gains a later event; the next import inserts only that one.
        source
            .insert_events(
                "aw-watcher-afk_localhost",
                vec![event(
                    9_000_000_000_000,
                    9_500_000_000_000,
                    r#"{"status":"not-afk"}"#,
                )],
            )
            .await
            .unwrap();
        let second = import_device(&source, &dest, "rugged").await.unwrap();
        assert_eq!(second.total_inserted(), 1, "only the new event imports");
        assert_eq!(
            events(&dest, "rugged::aw-watcher-afk_localhost")
                .await
                .len(),
            2
        );

        // No further source change: a third import adds nothing.
        assert_eq!(
            import_device(&source, &dest, "rugged")
                .await
                .unwrap()
                .total_inserted(),
            0
        );
    });
}

#[test]
fn catches_up_after_days_offline_from_last_destination_event() {
    runtime().block_on(async {
        let root = temp_root("offline-catchup");
        let (_source_server, source) = aw_server(&root, "source").await;
        let (_dest_server, dest) = aw_server(&root, "dest").await;

        let initial = event(
            1_700_000_000_000_000_000,
            1_700_000_001_000_000_000,
            r#"{"status":"not-afk"}"#,
        );
        seed_bucket(
            &source,
            "aw-watcher-afk_localhost",
            "afkstatus",
            "aw-watcher-afk",
            "localhost",
            vec![initial.clone()],
        )
        .await;
        assert_eq!(
            import_device_with_options_for_test(
                &source,
                &dest,
                "rugged",
                ImportOptions::from_lookback_seconds(0, false).unwrap(),
            )
            .await
            .unwrap()
            .total_inserted(),
            1
        );

        // Model a desktop used offline for multiple days. The next invocation
        // starts at the last destination event and must import the whole backlog,
        // not just a wall-clock-sized slice.
        let backlog: Vec<Event> = (1..=2_050)
            .map(|i| {
                let start = 1_700_000_002_000_000_000 + i * 86_400_000_000_000;
                event(
                    start,
                    start + 1_000_000_000,
                    &format!(r#"{{"status":"afk","day":{i}}}"#),
                )
            })
            .collect();
        source
            .insert_events("aw-watcher-afk_localhost", backlog.clone())
            .await
            .unwrap();

        let summary = import_device_with_options_for_test(
            &source,
            &dest,
            "rugged",
            ImportOptions::from_lookback_seconds(0, false).unwrap(),
        )
        .await
        .unwrap();
        assert_eq!(summary.total_inserted(), backlog.len());
        assert_eq!(
            events(&dest, "rugged::aw-watcher-afk_localhost")
                .await
                .len(),
            backlog.len() + 1
        );
    });
}

#[test]
fn padded_frontier_drops_clipped_old_events_and_keeps_exact_frontier() {
    runtime().block_on(async {
        let root = temp_root("frontier");
        let (_source_server, source) = aw_server(&root, "source").await;
        let (_dest_server, dest) = aw_server(&root, "dest").await;
        let base = 1_700_000_000_000_000_000;
        let old_crossing = event(base + 4_000_000_000, base + 9_000_000_000, r#"{"n":0}"#);
        let exact_frontier = event(base + 5_000_000_000, base + 6_000_000_000, r#"{"n":1}"#);
        let cursor = event(base + 10_000_000_000, base + 11_000_000_000, r#"{"n":2}"#);
        seed_bucket(
            &source,
            "aw-watcher-window_localhost",
            "currentwindow",
            "aw-watcher-window",
            "localhost",
            vec![old_crossing, exact_frontier.clone(), cursor.clone()],
        )
        .await;
        seed_bucket(
            &dest,
            "rugged::aw-watcher-window_localhost",
            "currentwindow",
            "aw-watcher-window",
            "rugged",
            vec![cursor],
        )
        .await;

        let summary = import_device_with_options_for_test(
            &source,
            &dest,
            "rugged",
            ImportOptions {
                lookback: TimeDelta::seconds(5),
                full_reconcile: false,
            },
        )
        .await
        .unwrap();
        assert_eq!(summary.total_inserted(), 1);
        let imported = events(&dest, "rugged::aw-watcher-window_localhost").await;
        assert_eq!(imported.len(), 2);
        assert!(
            imported
                .iter()
                .any(|e| e.timestamp.timestamp_nanos_opt() == Some(base + 5_000_000_000))
        );
        assert!(
            !imported
                .iter()
                .any(|e| e.timestamp.timestamp_nanos_opt() == Some(base + 5_000_000_000 - 1))
        );
    });
}

#[test]
fn full_reconcile_repairs_an_old_gap() {
    runtime().block_on(async {
        let root = temp_root("full-reconcile");
        let (_source_server, source) = aw_server(&root, "source").await;
        let (_dest_server, dest) = aw_server(&root, "dest").await;
        let source_events = vec![
            event(1_000_000_000_000, 1_500_000_000_000, r#"{"n":1}"#),
            event(2_000_000_000_000, 2_500_000_000_000, r#"{"n":2}"#),
            event(3_000_000_000_000, 3_500_000_000_000, r#"{"n":3}"#),
        ];
        seed_bucket(
            &source,
            "aw-watcher-window_localhost",
            "currentwindow",
            "aw-watcher-window",
            "localhost",
            source_events.clone(),
        )
        .await;
        seed_bucket(
            &dest,
            "rugged::aw-watcher-window_localhost",
            "currentwindow",
            "aw-watcher-window",
            "rugged",
            vec![source_events[2].clone()],
        )
        .await;

        let normal = import_device_with_options_for_test(
            &source,
            &dest,
            "rugged",
            ImportOptions::from_lookback_seconds(0, false).unwrap(),
        )
        .await
        .unwrap();
        assert_eq!(normal.total_inserted(), 0);

        let full = import_device_with_options_for_test(
            &source,
            &dest,
            "rugged",
            ImportOptions::from_lookback_seconds(0, true).unwrap(),
        )
        .await
        .unwrap();
        assert_eq!(full.total_inserted(), 2);
        assert!(full.buckets[0].full_reconcile);
    });
}

#[test]
fn imports_into_a_key_gated_central() {
    // The central will sit behind a bearer-gated route in production; prove the
    // importer authenticates when its dest client carries the token.
    runtime().block_on(async {
        let root = temp_root("auth");
        let (_source_server, source) = aw_server(&root, "source").await;
        let (_dest_server, dest) = aw_server_authed(&root, "dest", Some("s3cr3t-key")).await;

        seed_bucket(
            &source,
            "aw-watcher-afk_localhost",
            "afkstatus",
            "aw-watcher-afk",
            "localhost",
            vec![event(
                1_000_000_000_000,
                2_000_000_000_000,
                r#"{"status":"afk"}"#,
            )],
        )
        .await;

        // The dest client carries the bearer, so writes are accepted.
        assert_eq!(
            import_device(&source, &dest, "rugged")
                .await
                .unwrap()
                .total_inserted(),
            1
        );
        assert_eq!(
            events(&dest, "rugged::aw-watcher-afk_localhost")
                .await
                .len(),
            1
        );

        // A client without the bearer is rejected -- so the import above passed
        // because it authenticated, not because the gate was off.
        let unauth = connect(&dest.baseurl.to_string(), None, "unauth").expect("client");
        assert!(
            unauth.get_buckets().await.is_err(),
            "api_key gate is not enforced"
        );
    });
}

#[test]
fn large_bucket_inserts_across_batches() {
    // The importer POSTs a bucket's new events in bounded batches so a first
    // backfill is many small requests, not one oversized one. Seed more than one
    // batch worth, prove they all land (across >1 POST), and prove a re-import
    // across batches still adds nothing.
    runtime().block_on(async {
        let root = temp_root("batch");
        let (_source_server, source) = aw_server(&root, "source").await;
        let (_dest_server, dest) = aw_server(&root, "dest").await;

        let n = aw_importer::INSERT_BATCH_SIZE + 50;
        let seeded: Vec<Event> = (0..n as i64)
            .map(|i| {
                let start = 1_000_000_000_000 + i * 1_000_000;
                event(start, start + 500_000, &format!(r#"{{"app":"a","n":{i}}}"#))
            })
            .collect();
        seed_bucket(
            &source,
            "aw-watcher-window_localhost",
            "currentwindow",
            "aw-watcher-window",
            "localhost",
            seeded,
        )
        .await;

        let summary = import_device(&source, &dest, "rugged").await.unwrap();
        assert_eq!(summary.total_inserted(), n);
        assert_eq!(
            events(&dest, "rugged::aw-watcher-window_localhost")
                .await
                .len(),
            n
        );

        // Re-import across batches is still idempotent.
        assert_eq!(
            import_device(&source, &dest, "rugged")
                .await
                .unwrap()
                .total_inserted(),
            0
        );
    });
}
