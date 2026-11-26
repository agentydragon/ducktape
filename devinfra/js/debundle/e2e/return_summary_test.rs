//! A call's return relationship must not import another call's callback.

use debundle_e2e_graph_support::{FixtureGraphExt, has_promoted_read};
use debundle_e2e_support::saved_analysis::{
    assert_same_invocation_trees, assert_staged_entry_output, path, staged,
};
use debundle_e2e_support::*;

fn independent_callbacks(wrapper: &str, callee: &str, namespace: bool) {
    let input = |callback: &str| {
        if namespace {
            format!("{{ __esModule: true, default: {callback} }}")
        } else {
            callback.to_string()
        }
    };
    let source = format!(
        r#"
{wrapper}
const earlier = 41;
function selectedCallback() {{ return earlier; }}
function unrelatedCallback() {{ return later; }}
const selectedInput = {};
const unrelatedInput = {};
const selected = {callee}(selectedInput);
const unrelated = {callee}(unrelatedInput);
const observed = selected();
const later = 7;
console.log(observed, unrelated());
"#,
        input("selectedCallback"),
        input("unrelatedCallback"),
    );
    let (stage, _fixture) = staged(
        FixtureOpts::new(
            &source,
            if namespace || callee != "relay" {
                // Guarded and nested calls also need effect/normal-return proofs.
                // A source return relation alone does not grant those.
                vec![]
            } else {
                vec![logical_module(
                    "selected",
                    &[
                        Member::new("relay"),
                        Member::new("earlier"),
                        Member::new("selectedCallback"),
                        Member::new("selectedInput"),
                        Member::new("selected"),
                        Member::new("observed"),
                    ],
                )]
            },
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    for invocation in stage.assert_fresh_saved_parity(&[], 0, "41 7\n", "41 7\n") {
        let graph = invocation.dependency_graph();
        assert!(
            has_promoted_read(&graph, "observed", "earlier", "selectedCallback"),
            "the selected callback's read was lost"
        );
        assert!(
            !has_promoted_read(&graph, "observed", "later", "unrelatedCallback"),
            "an independent return polluted the selected call"
        );
    }
}

#[test]
fn identity_result_allows_an_independent_initializer_to_move() {
    independent_callbacks("function relay(value) { return value; }", "relay", false);
}

#[test]
fn nested_calls_with_the_same_start_keep_their_own_arguments() {
    independent_callbacks(
        "function relay(value) { return value; }",
        "relay(relay)",
        false,
    );
}

#[test]
fn arrow_identity_result_allows_an_independent_initializer_to_move() {
    independent_callbacks("const relay = value => value;", "relay", false);
}

#[test]
fn default_export_result_keeps_independent_callback_origins() {
    independent_callbacks(
        "function relay(value) { return value && value.__esModule && Object.prototype.hasOwnProperty.call(value, 'default') ? value.default : value; }",
        "relay",
        true,
    );
}

fn assert_unsafe_return_summary_cut(
    source: &str,
    members: &[&'static str],
    callback_setup: &[&str],
    original_output: &str,
    emitted_output: &str,
) {
    let (stage, _fixture) = staged(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "callbacks/selected",
                    &members.iter().copied().map(Member::new).collect::<Vec<_>>(),
                    callback_setup,
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    // An unsupported return relationship must keep the unsafe cut rejected.
    stage.assert_fresh_saved_parity(&["--emit-on-rejection"], 3, original_output, emitted_output);
}

fn assert_eval_admission_refused(source: &str, members: &[&'static str], original_output: &str) {
    let (stage, _fixture) = staged(
        FixtureOpts::new(
            source,
            vec![
                logical_module(
                    "callbacks/selected",
                    &members.iter().copied().map(Member::new).collect::<Vec<_>>(),
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_node_output(
        &stage.root().join("snapshot/static/app.js"),
        original_output,
        "",
    );
    let fresh = stage.run(&[]);
    let saved = stage.run(&["--analysis", path(&stage.artifact)]);
    for invocation in [&fresh, &saved] {
        assert_eq!(
            invocation.result.status.code(),
            Some(1),
            "direct eval must keep input admission rejected:\n{}",
            invocation.result.stderr
        );
        assert!(invocation.result.stderr.contains("[a1_eval]"));
    }
    assert_same_invocation_trees(&fresh, &saved);
}

#[test]
fn identity_after_property_write_preserves_the_introduced_callback_dependency() {
    assert_unsafe_return_summary_cut(
        r#"
let later = 0;
function relay(value) {
  value.extra = () => later;
  return value;
}
const selected = relay(() => 1);
const observed = selected.extra();
later = 42;
console.log(observed, later);
"#,
        &["relay", "selected", "observed"],
        &[],
        "0 42\n",
        "42 42\n",
    );
}

#[test]
fn parameter_rebinding_does_not_use_a_first_argument_return_summary() {
    assert_unsafe_return_summary_cut(
        r#"
let later = 0;
function relay(value) {
  value = () => later;
  return value;
}
const selected = relay(() => 1);
const observed = selected();
later = 42;
console.log(observed, later);
"#,
        &["relay", "selected", "observed"],
        &[],
        "0 42\n",
        "42 42\n",
    );
}

#[test]
fn nested_closure_parameter_write_does_not_use_a_first_argument_return_summary() {
    assert_unsafe_return_summary_cut(
        r#"
let later = 0;
function relay(value) {
  function replace() { value = () => later; }
  replace();
  return value;
}
const selected = relay(() => 1);
const observed = selected();
later = 42;
console.log(observed, later);
"#,
        &["relay", "selected", "observed"],
        &[],
        "0 42\n",
        "42 42\n",
    );
}

#[test]
fn default_parameter_return_does_not_use_the_call_argument_summary() {
    assert_unsafe_return_summary_cut(
        r#"
let later = 0;
function relay(value = () => later) { return value; }
const selected = relay(undefined);
const observed = selected();
later = 42;
console.log(observed, later);
"#,
        &["relay", "selected", "observed"],
        &[],
        "0 42\n",
        "42 42\n",
    );
}

#[test]
fn body_function_declaration_does_not_preserve_the_parameter_return() {
    assert_unsafe_return_summary_cut(
        r#"
let later = 0;
function relay(value) {
  function value() { return later; }
  return value;
}
const selected = relay(() => 1);
const observed = selected();
later = 42;
console.log(observed, later);
"#,
        &["relay", "selected", "observed"],
        &[],
        "0 42\n",
        "42 42\n",
    );
}

#[test]
fn var_initializer_does_not_preserve_the_parameter_return() {
    assert_unsafe_return_summary_cut(
        r#"
let later = 0;
function relay(value) {
  var value = () => later;
  return value;
}
const selected = relay(() => 1);
const observed = selected();
later = 42;
console.log(observed, later);
"#,
        &["relay", "selected", "observed"],
        &[],
        "0 42\n",
        "42 42\n",
    );
}

#[test]
fn direct_eval_parameter_write_does_not_use_a_first_argument_return_summary() {
    assert_eval_admission_refused(
        r#"
let later = 0;
function relay(value) {
  eval("value = () => later");
  return value;
}
const selected = relay(() => 1);
const observed = selected();
later = 42;
console.log(observed, later);
"#,
        &["relay", "selected", "observed"],
        "0 42\n",
    );
}

#[test]
fn forwarding_cycle_does_not_use_a_first_argument_return_summary() {
    assert_unsafe_return_summary_cut(
        r#"
let later = 0;
let chooseCycle = false;
function relay(value) { return chooseCycle ? forward(value) : replacement(value); }
function forward(value) { return relay(value); }
function replacement(value) { return () => later; }
const selected = relay(() => 1);
const observed = selected();
later = 42;
console.log(observed, later);
"#,
        &[
            "relay",
            "forward",
            "replacement",
            "selected",
            "observed",
            "chooseCycle",
        ],
        &[],
        "0 42\n",
        "42 42\n",
    );
}

#[test]
fn reassigned_helper_binding_does_not_keep_a_stale_argument_summary() {
    assert_unsafe_return_summary_cut(
        r#"
let later = 0;
let chooseReplacement = true;
function stable(value) { return value; }
function relay(value) { return chooseReplacement ? invoke(value) : stable(value); }
function replacement(value) { return () => later; }
let invoke = stable;
invoke = replacement;
const selected = relay(() => 1);
const observed = selected();
later = 42;
console.log(observed, later);
"#,
        &[
            "relay",
            "stable",
            "replacement",
            "invoke",
            "selected",
            "observed",
            "chooseReplacement",
        ],
        &["invoke = replacement;"],
        "0 42\n",
        "42 42\n",
    );
}

#[test]
fn conditional_effectful_identity_helpers_preserve_returned_callback_and_effects() {
    let source = r#"
const earlier = 41;
let effects = 0;
function afterEffect(value) {
  effects += 1;
  return value;
}
function relay(value) {
  return value ? afterEffect(value) : afterEffect(value);
}
function selectedCallback() { return earlier; }
const selected = relay(selectedCallback);
const observed = selected();
const later = 7;
console.log(observed, effects, later);
"#;
    assert_staged_entry_output(
        FixtureOpts::new(
            source,
            vec![
                logical_module(
                    "callbacks/selected",
                    &[
                        Member::new("earlier"),
                        Member::new("effects"),
                        Member::new("afterEffect"),
                        Member::new("relay"),
                        Member::new("selectedCallback"),
                        Member::new("selected"),
                        Member::new("observed"),
                    ],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        "41 1 7\n",
    );
}
