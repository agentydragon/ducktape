//! Empty intrinsic collections cannot invoke callbacks during construction.

use debundle_e2e_support::*;

fn collection_opts<'a>(source: &'a str, members: &[Member], anonymous: &[&str]) -> FixtureOpts<'a> {
    FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon("values/collection", members, anonymous),
            logical_module("values/later", &[Member::new("later")]),
        ],
    )
}

fn assert_empty_collection(constructor: &str) {
    let source = format!(
        r#"globalThis.saved = () => later;
const collection = new {constructor}();
const later = 42;
console.log(collection instanceof {constructor}, later);
"#
    );
    let fixture = run_fixture(collection_opts(
        &source,
        &[Member::new("collection")],
        &["globalThis.saved = () => later;"],
    ));
    assert_preserved_entry_output(&fixture, "true 42\n");
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn empty_map_cannot_reenter_published_callback() {
    assert_empty_collection("Map");
}

#[test]
fn empty_set_cannot_reenter_published_callback() {
    assert_empty_collection("Set");
}

#[test]
fn empty_weak_map_cannot_reenter_published_callback() {
    assert_empty_collection("WeakMap");
}

#[test]
fn empty_weak_set_cannot_reenter_published_callback() {
    assert_empty_collection("WeakSet");
}

#[test]
fn shadowed_map_constructor_can_reenter() {
    let source = r#"globalThis.saved = () => later;
function Map() {
  try { globalThis.saved(); return { value: "ready" }; }
  catch (error) { return { value: error.name }; }
}
const collection = new Map();
const later = 42;
console.log(collection.value);
"#;
    expect_atomic_conflict_rejection(
        collection_opts(
            source,
            &[Member::new("Map"), Member::new("collection")],
            &["globalThis.saved = () => later;"],
        ),
        &["values/collection", "values/later"],
        &[],
    );
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "ReferenceError\n");
}

#[test]
fn map_iterable_can_reenter() {
    let source = r#"globalThis.saved = () => later;
const items = { [Symbol.iterator]() {
  try { globalThis.saved(); } catch (error) { globalThis.observed = error.name; }
  return [][Symbol.iterator]();
} };
const collection = new Map(items);
const later = 42;
console.log(globalThis.observed, collection.size);
"#;
    expect_atomic_conflict_rejection(
        collection_opts(
            source,
            &[Member::new("items"), Member::new("collection")],
            &["globalThis.saved = () => later;"],
        ),
        &["values/collection", "values/later"],
        &[],
    );
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "ReferenceError 0\n");
}
