//! Standard Set consumes proven fresh arrays without invoking chunk callbacks.

use debundle_e2e_support::*;

const ALIAS_SOURCE: &str = r#"const values = ["a", "b"];
const alias = values;
globalThis.saved = () => later;
const unique = new Set(alias);
const later = 42;
console.log(unique.size, later);
"#;

const INLINE_SOURCE: &str = r#"globalThis.saved = () => later;
const unique = new Set(["a", "b"]);
const later = 42;
console.log(unique.size, later);
"#;

const CUSTOM_ITERATOR_SOURCE: &str = r#"const values = ["a"];
globalThis.saved = () => later;
values[Symbol.iterator] = function () { let done = false; return { next() { if (done) return { done: true }; done = true; try { globalThis.saved(); } catch (error) { return { value: error.name, done: false }; } return { value: "a", done: false }; } }; };
const unique = new Set(values);
const later = 42;
console.log(unique.has("ReferenceError"));
"#;

const SHADOWED_SET_SOURCE: &str = r#"const values = ["a"];
globalThis.saved = () => later;
function Set(items) {
  try { globalThis.saved(); return {}; }
  catch (error) { return { value: error.name }; }
}
const unique = new Set(values);
const later = 42;
console.log(unique.value);
"#;

const CAPTURED_FUNCTION_SET_SOURCE: &str = r#"globalThis.consume = function (values) {
  for (const callback of values) {
    try { callback(); } catch (error) { return error.name; }
  }
};
const collection = new Set([() => later]);
console.log(globalThis.consume(collection));
const later = 42;
"#;

const ALIAS_MUTATION_SOURCE: &str = r#"globalThis.consume = function (values) {
  for (const callback of values) {
    try { callback(); } catch (error) { return error.name; }
  }
};
const lookup = { key: "token" }, alias = lookup;
alias.key = () => later;
const collection = new Set([lookup.key]);
console.log(globalThis.consume(collection));
const later = 42;
"#;

const OPAQUE_ALIAS_MUTATION_SOURCE: &str = r#"globalThis.consume = function (values) {
  for (const callback of values) {
    try { callback(); } catch (error) { return error.name; }
  }
};
const lookup = { key: "token" }, alias = Reflect.apply((value) => value, null, [lookup]);
alias.key = () => later;
const collection = new Set([lookup.key]);
console.log(globalThis.consume(collection));
const later = 42;
"#;

fn split_opts(source: &'static str, collection: LogicalModuleEntry) -> FixtureOpts<'static> {
    FixtureOpts::new(
        source,
        vec![
            collection,
            logical_module("values/later", &[Member::new("later")]),
        ],
    )
    .with_chunk_id("static/provider")
}

fn assert_split_runs(source: &'static str, collection: LogicalModuleEntry, expected: &str) {
    let fixture = run_fixture(split_opts(source, collection));
    assert_preserved_entry_output(&fixture, expected);
    assert_all_emitted_js_checks(&fixture);
}

fn assert_real_callback_keeps_split_rejected(
    source: &'static str,
    collection: LogicalModuleEntry,
    expected: &str,
) {
    let rejected = run_rejection_fixture(split_opts(source, collection));
    assert!(
        rejected.stderr.contains("materialize_logical_modules"),
        "expected a materialization rejection, got: {}",
        rejected.stderr,
    );

    let fixture = run_fixture(FixtureOpts::new(source, vec![]).with_chunk_id("static/provider"));
    assert_preserved_entry_output(&fixture, expected);
}

#[test]
fn unescaped_const_primitive_array_alias_does_not_fabricate_set_reentry() {
    assert_split_runs(
        ALIAS_SOURCE,
        logical_module_with_anon(
            "values/collection",
            &[
                Member::new("values"),
                Member::new("alias"),
                Member::new("unique"),
            ],
            &["globalThis.saved = () => later;"],
        ),
        "2 42\n",
    );
}

#[test]
fn inline_array_literal_does_not_fabricate_set_reentry() {
    assert_split_runs(
        INLINE_SOURCE,
        logical_module_with_anon(
            "values/collection",
            &[Member::new("unique")],
            &["globalThis.saved = () => later;"],
        ),
        "2 42\n",
    );
}

#[test]
fn custom_array_iterator_callback_still_blocks_split() {
    assert_real_callback_keeps_split_rejected(
        CUSTOM_ITERATOR_SOURCE,
        logical_module_with_anon(
            "values/collection",
            &[Member::new("values"), Member::new("unique")],
            &[
                "globalThis.saved = () => later;",
                "values[Symbol.iterator] = function () { let done = false; return { next() { if (done) return { done: true }; done = true; try { globalThis.saved(); } catch (error) { return { value: error.name, done: false }; } return { value: \"a\", done: false }; } }; };",
            ],
        ),
        "true\n",
    );
}

#[test]
fn shadowed_set_callback_still_blocks_split() {
    let collection = logical_module_with_anon(
        "values/collection",
        &[
            Member::new("values"),
            Member::new("Set"),
            Member::new("unique"),
        ],
        &["globalThis.saved = () => later;"],
    );
    expect_atomic_conflict_rejection(
        split_opts(SHADOWED_SET_SOURCE, collection),
        &["values/collection", "values/later"],
        &[],
    );
    let fixture =
        run_fixture(FixtureOpts::new(SHADOWED_SET_SOURCE, vec![]).with_chunk_id("static/provider"));
    assert_preserved_entry_output(&fixture, "ReferenceError\n");
}

#[test]
fn set_retaining_callback_still_blocks_split() {
    assert_real_callback_keeps_split_rejected(
        CAPTURED_FUNCTION_SET_SOURCE,
        logical_module_with_anon(
            "values/collection",
            &[Member::new("collection")],
            &[
                "globalThis.consume = function (values) { for (const callback of values) { try { callback(); } catch (error) { return error.name; } } };",
            ],
        ),
        "ReferenceError\n",
    );
}

#[test]
fn aliased_member_mutation_retains_callback() {
    assert_real_callback_keeps_split_rejected(
        ALIAS_MUTATION_SOURCE,
        logical_module_with_anon(
            "values/collection",
            &[
                Member::new("lookup"),
                Member::new("alias"),
                Member::new("collection"),
            ],
            &[
                "globalThis.consume = function (values) { for (const callback of values) { try { callback(); } catch (error) { return error.name; } } };",
                "alias.key = () => later;",
            ],
        ),
        "ReferenceError\n",
    );
}

#[test]
fn opaque_alias_member_mutation_retains_callback() {
    assert_real_callback_keeps_split_rejected(
        OPAQUE_ALIAS_MUTATION_SOURCE,
        logical_module_with_anon(
            "values/collection",
            &[
                Member::new("lookup"),
                Member::new("alias"),
                Member::new("collection"),
            ],
            &[
                "globalThis.consume = function (values) { for (const callback of values) { try { callback(); } catch (error) { return error.name; } } };",
                "alias.key = () => later;",
            ],
        ),
        "ReferenceError\n",
    );
}
