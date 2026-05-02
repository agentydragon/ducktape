//! End-to-end tests for the scrambled-identifier frequency queue side
//! output. Drives the real pipeline through a synthetic minified bundle
//! and asserts on the JSON contract.

use debundle_e2e_support::*;
use serde_json::Value;
use std::fs;

#[test]
fn emits_a_frequency_queue_alongside_the_write_tree_manifest() {
    // The synthetic bundle has three top-level scrambled bindings:
    //   - `aH` is referenced 5x within the entry (inside `bC`, `dE`,
    //     and the trailing console.log) so it tops the queue.
    //   - `bC` (a function) references `aH` and is itself referenced 2x.
    //   - `dE` references both `aH` and `bC` and is referenced 1x.
    //   - `getUserData` is camelCase developer-readable and MUST be
    //     omitted from the queue.
    //   - `URL` is a builtin acronym shape, but it's a reference to the
    //     global, NOT a top-level binding, so it just doesn't show up.
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"const aH = "scrambled-most-referenced";
function bC() { return aH + aH; }
const dE = bC() + aH + aH;
function getUserData() { return aH; }
console.log(dE, bC(), getUserData());
export { aH, bC, dE };
"#,
        vec![],
    ));

    let queue = read_queue(&fixture.out_root);
    assert_eq!(queue["schema_version"], 1);
    assert_eq!(queue["kind"], "js.scrambled_identifier_frequencies");
    assert!(
        queue["generated_at_iso"]
            .as_str()
            .expect("generated_at_iso should be a string")
            .ends_with('Z'),
        "expected ISO timestamp ending in Z, got {:?}",
        queue["generated_at_iso"]
    );

    let entries = queue["entries"]
        .as_array()
        .expect("entries must be an array");
    let names: Vec<&str> = entries
        .iter()
        .map(|e| e["scrambled_name"].as_str().unwrap())
        .collect();
    // `getUserData` must be filtered out (developer-readable).
    assert!(
        !names.contains(&"getUserData"),
        "developer-readable getUserData leaked into queue: {names:?}"
    );

    // All three scrambled top-level bindings must appear.
    for needle in ["aH", "bC", "dE"] {
        assert!(
            names.contains(&needle),
            "missing scrambled name {needle:?} in queue: {names:?}"
        );
    }

    // Queue must be sorted by ref_count desc with fanout_modules tiebreak.
    let ref_counts: Vec<u64> = entries
        .iter()
        .map(|e| e["ref_count"].as_u64().unwrap())
        .collect();
    let mut sorted = ref_counts.clone();
    sorted.sort_by(|a, b| b.cmp(a));
    assert_eq!(
        ref_counts, sorted,
        "entries must be sorted by ref_count descending"
    );

    // Selector is the documented stable-id triple form.
    for entry in entries {
        let selector = entry["selector"].as_str().expect("selector must be string");
        let parts: Vec<&str> = selector.split(':').collect();
        assert_eq!(
            parts.len(),
            3,
            "selector must be <chunk>:<file>:<ordinal> form, got {selector}",
        );
        // ordinal is a non-negative integer
        let _ordinal: usize = parts[2]
            .parse()
            .unwrap_or_else(|e| panic!("selector ordinal must parse as usize: {selector}: {e}"));
        // owner_chunk and owner_file are populated.
        assert!(!entry["owner_chunk"].as_str().unwrap().is_empty());
        assert!(!entry["owner_file"].as_str().unwrap().is_empty());
    }

    // total_references is the sum of the per-entry ref_count.
    let total_refs: u64 = ref_counts.iter().sum();
    assert_eq!(queue["total_references"].as_u64().unwrap(), total_refs);
    assert_eq!(
        queue["total_scrambled_symbols"].as_u64().unwrap(),
        entries.len() as u64
    );

    // The first entry should be `aH` — it has the highest reference
    // count (5 reads inside the synthetic bundle's body and exports).
    assert_eq!(
        entries[0]["scrambled_name"].as_str().unwrap(),
        "aH",
        "expected highest ref_count to be `aH`; entries: {entries:#?}",
    );
}

#[test]
fn manifest_records_the_queue_path_at_a_relative_path() {
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"const xY = 1;
console.log(xY);
export { xY };
"#,
        vec![],
    ));
    let manifest = read_manifest(&fixture.out_root);
    let path = manifest["scrambledIdentifierFrequencies"]
        .as_str()
        .expect("manifest must record scrambledIdentifierFrequencies path");
    assert!(
        !path.starts_with('/') && !path.starts_with(".."),
        "queue path must be manifest-relative, got {path}",
    );
    let resolved = fixture.out_root.join(path);
    assert!(
        resolved.exists(),
        "queue path {path} resolves to {resolved:?} which does not exist",
    );
}

#[test]
fn entries_are_byte_identical_across_repeated_runs_against_same_inputs() {
    // Stable-selector test: re-running the pipeline against the same
    // synthetic bundle must yield byte-identical `entries` payloads
    // (selectors don't drift, sort is deterministic).
    let first = read_entries_payload(run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"const aH = "stable";
function bC() { return aH; }
const dE = bC();
console.log(dE);
export { aH, bC, dE };
"#,
        vec![],
    )));
    let second = read_entries_payload(run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"const aH = "stable";
function bC() { return aH; }
const dE = bC();
console.log(dE);
export { aH, bC, dE };
"#,
        vec![],
    )));
    assert_eq!(
        first, second,
        "entries payload must be byte-identical across runs",
    );
}

fn read_queue(out_root: &std::path::Path) -> Value {
    let path = out_root.join("scrambled-identifier-frequencies.json");
    let raw = fs::read_to_string(&path).unwrap_or_else(|e| panic!("read {}: {e}", path.display()));
    serde_json::from_str(&raw).expect("queue JSON must parse")
}

fn read_manifest(out_root: &std::path::Path) -> Value {
    let path = out_root.join("manifest.json");
    let raw = fs::read_to_string(&path).unwrap_or_else(|e| panic!("read {}: {e}", path.display()));
    serde_json::from_str(&raw).expect("manifest JSON must parse")
}

/// Just the `entries` field — `generated_at_iso` is wall-clock-derived
/// and intentionally varies.
fn read_entries_payload(fixture: Fixture) -> Value {
    let queue = read_queue(&fixture.out_root);
    queue["entries"].clone()
}
