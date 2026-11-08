//! A callback that reads an unavailable future binding must stop at its TDZ
//! error. It must not inherit deferred reads from the binding's initializer.

use debundle_e2e_support::*;
use std::fmt::Write as _;

const PEER: [(&str, &str); 1] = [(
    "static/peer",
    r#"export function host(...callbacks) {
  const observations = [];
  for (const callback of callbacks) {
    try {
      observations.push(callback());
    } catch (error) {
      observations.push(error.name);
    }
  }
  return observations;
}
"#,
)];

/// One callback plus 64 fillers exceeds the bounded synchronous callback pool.
const FILLER_CALLBACKS: usize = 64;

fn source(callback_setup: &str, callback: &str, tail: &str) -> String {
    let mut result = format!("import {{ host }} from './peer.js';\n{callback_setup}\n");
    let mut callbacks = vec![callback.to_string()];
    for index in 0..FILLER_CALLBACKS {
        writeln!(result, "function filler{index}() {{ return {index}; }}").unwrap();
        callbacks.push(format!("filler{index}"));
    }
    writeln!(
        result,
        "const observations = host({});",
        callbacks.join(", ")
    )
    .unwrap();
    result.push_str(tail);
    result
}

#[test]
fn future_initializer_callback_does_not_add_a_style_read_to_fallback() {
    let input = source(
        "function wrap(callback) { return callback; }\n\
         function readFuture() { return future(); }",
        "readFuture",
        "const future = wrap(function () { return styles.value; });\n\
         const styles = { value: 'ready' };\n\
         console.log(observations[0], future());\n",
    );
    let fixture = run_fixture(
        FixtureOpts::new(
            &input,
            vec![logical_module("values/styles", &[Member::new("styles")])],
        )
        .with_extra_chunks(&PEER)
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "ReferenceError ready\n");
}

#[test]
fn callback_created_before_host_keeps_its_real_style_tdz_dependency() {
    let input = source(
        "function readStyles() { return styles.value; }",
        "readStyles",
        "const styles = { value: 'ready' };\n\
         console.log(observations[0], readStyles());\n",
    );
    let colocated = run_fixture(
        FixtureOpts::new(&input, Vec::new())
            .with_extra_chunks(&PEER)
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&colocated, "ReferenceError ready\n");

    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &input,
            vec![logical_module("values/styles", &[Member::new("styles")])],
        )
        .with_extra_chunks(&PEER)
        .with_unassigned_mode(unassigned_mode_inline()),
        &["values/styles"],
        &[],
    );
}

#[test]
fn future_binding_tdz_remains_when_future_itself_is_moved() {
    let input = source(
        "function readFuture() { return future(); }",
        "readFuture",
        "const future = function () { return 'ready'; };\n\
         console.log(observations[0]);\n",
    );
    let colocated = run_fixture(
        FixtureOpts::new(&input, Vec::new())
            .with_extra_chunks(&PEER)
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&colocated, "ReferenceError\n");

    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &input,
            vec![logical_module("values/future", &[Member::new("future")])],
        )
        .with_extra_chunks(&PEER)
        .with_unassigned_mode(unassigned_mode_inline()),
        &["values/future"],
        &[],
    );
}

#[test]
fn fallback_revisits_future_callback_after_its_binding_initializes() {
    let input = source(
        "function wrap(callback) { return callback; }\n\
         function readFuture() { return future(); }",
        "readFuture",
        "const future = wrap(function () { return styles.value; });\n\
         const laterObservations = host(readFuture);\n\
         const styles = { value: 'ready' };\n\
         console.log(observations[0], laterObservations[0], future());\n",
    );
    let colocated = run_fixture(
        FixtureOpts::new(&input, Vec::new())
            .with_extra_chunks(&PEER)
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&colocated, "ReferenceError ReferenceError ready\n");

    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &input,
            vec![logical_module("values/styles", &[Member::new("styles")])],
        )
        .with_extra_chunks(&PEER)
        .with_unassigned_mode(unassigned_mode_inline()),
        &["values/styles"],
        &[],
    );
}

#[test]
fn callback_created_in_current_initializer_keeps_its_style_tdz_dependency() {
    let input = source(
        "",
        "function () { return styles.value; }",
        "const styles = { value: 'ready' };\n\
         console.log(observations[0], styles.value);\n",
    );
    let colocated = run_fixture(
        FixtureOpts::new(&input, Vec::new())
            .with_extra_chunks(&PEER)
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&colocated, "ReferenceError ready\n");

    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &input,
            vec![logical_module("values/styles", &[Member::new("styles")])],
        )
        .with_extra_chunks(&PEER)
        .with_unassigned_mode(unassigned_mode_inline()),
        &["values/styles"],
        &[],
    );
}
