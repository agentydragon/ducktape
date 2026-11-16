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
const FILLER_BINDINGS: &[&str] = &[
    "filler0", "filler1", "filler2", "filler3", "filler4", "filler5", "filler6", "filler7",
    "filler8", "filler9", "filler10", "filler11", "filler12", "filler13", "filler14", "filler15",
    "filler16", "filler17", "filler18", "filler19", "filler20", "filler21", "filler22", "filler23",
    "filler24", "filler25", "filler26", "filler27", "filler28", "filler29", "filler30", "filler31",
    "filler32", "filler33", "filler34", "filler35", "filler36", "filler37", "filler38", "filler39",
    "filler40", "filler41", "filler42", "filler43", "filler44", "filler45", "filler46", "filler47",
    "filler48", "filler49", "filler50", "filler51", "filler52", "filler53", "filler54", "filler55",
    "filler56", "filler57", "filler58", "filler59", "filler60", "filler61", "filler62", "filler63",
];

fn source(callback_setup: &str, callback: &str, tail: &str) -> String {
    source_with_host(callback_setup, callback, tail, true)
}

fn source_with_host(callback_setup: &str, callback: &str, tail: &str, imported: bool) -> String {
    let host = if imported {
        "import { host } from './peer.js';"
    } else {
        PEER[0].1.strip_prefix("export ").unwrap()
    };
    let mut result = format!("{host}\n{callback_setup}\n");
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

fn all_bindings(imported: bool) -> Vec<Member> {
    let mut members = Vec::with_capacity(FILLER_CALLBACKS + 6);
    if !imported {
        members.push(Member::new("host"));
    }
    members.extend(FILLER_BINDINGS.iter().map(|&name| Member::new(name)));
    members.extend([
        Member::new("wrap"),
        Member::new("readFuture"),
        Member::new("observations"),
        Member::new("future"),
        Member::new("styles"),
    ]);
    members
}

fn split_styles(input: &str, imported: bool) -> FixtureOpts<'_> {
    FixtureOpts::new(
        input,
        vec![logical_module("values/styles", &[Member::new("styles")])],
    )
    .with_extra_chunks(if imported { &PEER[..] } else { &[] })
    .with_unassigned_mode(unassigned_mode_inline())
}

fn assert_schedule_refusal_preserves_source_and_candidate(
    opts: FixtureOpts<'_>,
    expected_output: &str,
) -> RejectedFixture {
    let rejected = run_rejection_fixture_with_graphs_and_args(opts, &["--emit-on-rejection"], 3);
    assert!(
        rejected
            .stderr
            .contains("evaluation_schedule_unknown_reordered"),
        "expected a conservative evaluation-schedule refusal:\n{}",
        rejected.stderr
    );
    assert_node_output(
        &rejected.source_root.join("static/app.js"),
        expected_output,
        "",
    );
    assert_node_output(&rejected.entry_path(), expected_output, "");
    rejected
}

#[test]
fn future_initializer_callback_split_keeps_style_timing_unproved() {
    for (declaration, imported) in [
        ("const", false),
        ("let", false),
        ("const", true),
        ("let", true),
    ] {
        let input = source_with_host(
            "function wrap(callback) { return callback; }\n\
         function readFuture() { return future(); }",
            "readFuture",
            &format!(
                "{declaration} future = wrap(function () {{ return styles.value; }});\n\
         const styles = {{ value: 'ready' }};\n\
         console.log(observations[0], future());\n"
            ),
            imported,
        );
        let rejected = assert_schedule_refusal_preserves_source_and_candidate(
            split_styles(&input, imported),
            "ReferenceError ready\n",
        );
        assert_module_exports(
            &rejected.app_root(),
            "static/app/modules/values/styles.js",
            &["styles"],
            &[],
        );

        let fixture = run_fixture(
            FixtureOpts::new(
                &input,
                vec![logical_module("runtime/all", &all_bindings(imported))],
            )
            .with_extra_chunks(if imported { &PEER[..] } else { &[] })
            .with_unassigned_mode(unassigned_mode_inline()),
        );
        assert_preserved_entry_output(&fixture, "ReferenceError ready\n");
        assert_module_source(
            &fixture.out_root,
            "static/app/modules/runtime/all.js",
            &["readFuture", "future = wrap", "const styles"],
            &[],
        );
    }
}

#[test]
fn later_hoisted_function_keeps_its_real_style_tdz_dependency() {
    let input = source(
        "function readFuture() { return future(); }",
        "readFuture",
        "function future() { return styles.value; }\n\
         const styles = { value: 'ready' };\n\
         console.log(observations[0], future());\n",
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
