//! A callback that reads an unavailable future binding must stop at its TDZ
//! error. It must not inherit deferred reads from the binding's initializer.

use super::{assert_unmoved_output, expect_single_binding_rejection};
use debundle_e2e_support::saved_analysis::{assert_same_invocation_trees, path, staged};
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

fn assert_availability_parity(
    input: &str,
    binding: &'static str,
    exit: i32,
    original: &str,
    candidate: &str,
) {
    let destination = format!("values/{binding}");
    let (staged, _fixture) = staged(
        FixtureOpts::new(
            input,
            vec![logical_module(&destination, &[Member::new(binding)])],
        )
        .with_extra_chunks(&PEER)
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_node_output(&staged.root().join("snapshot/static/app.js"), original, "");
    let direct = staged.run(&["--emit-on-rejection"]);
    let saved = staged.run(&["--emit-on-rejection", "--analysis", path(&staged.artifact)]);
    assert_same_invocation_trees(&direct, &saved);
    for invocation in [&direct, &saved] {
        assert_eq!(
            invocation.result.status.code(),
            Some(exit),
            "{}",
            invocation.result.stderr
        );
        let root = invocation.output_root().join("app");
        assert_node_output(&root.join("static/app/entry.js"), candidate, "");
        if exit == 0 {
            assert_module_exports(
                &root,
                &format!("static/app/modules/{destination}.js"),
                &[binding],
                &[],
            );
        }
    }
}

#[test]
fn future_initializer_callback_does_not_add_a_style_read_to_fallback() {
    for declaration in ["const", "let"] {
        let input = source(
            "function wrap(callback) { return callback; }\n\
         function readFuture() { return future(); }",
            "readFuture",
            &format!(
                "{declaration} future = wrap(function () {{ return styles.value; }});\n\
         const styles = {{ value: 'ready' }};\n\
         console.log(observations[0], future());\n"
            ),
        );
        assert_availability_parity(
            &input,
            "styles",
            0,
            "ReferenceError ready\n",
            "ReferenceError ready\n",
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
    assert_unmoved_output(&input, &PEER, "ReferenceError ready\n");

    assert_availability_parity(
        &input,
        "styles",
        3,
        "ReferenceError ready\n",
        "ready ready\n",
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
    assert_unmoved_output(&input, &PEER, "ReferenceError ready\n");
    expect_single_binding_rejection(&input, &PEER, "values/styles", "styles");
}

#[test]
fn future_binding_tdz_remains_when_future_itself_is_moved() {
    let input = source(
        "function readFuture() { return future(); }",
        "readFuture",
        "const future = function () { return 'ready'; };\n\
         console.log(observations[0]);\n",
    );
    assert_unmoved_output(&input, &PEER, "ReferenceError\n");

    assert_availability_parity(&input, "future", 3, "ReferenceError\n", "ready\n");
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
    assert_unmoved_output(&input, &PEER, "ReferenceError ReferenceError ready\n");
    expect_single_binding_rejection(&input, &PEER, "values/styles", "styles");
}

#[test]
fn callback_created_in_current_initializer_keeps_its_style_tdz_dependency() {
    let input = source(
        "",
        "function () { return styles.value; }",
        "const styles = { value: 'ready' };\n\
         console.log(observations[0], styles.value);\n",
    );
    assert_unmoved_output(&input, &PEER, "ReferenceError ready\n");
    expect_single_binding_rejection(&input, &PEER, "values/styles", "styles");
}
