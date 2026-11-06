//! Export bindings become externally reachable according to their ESM initialization timing.

use analysis_model::DepKind;
use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::*;

fn binding_execution_report(fixture: &Fixture, binding: &str) -> serde_json::Value {
    let graph = fixture.dependency_graph();
    let unit = source_unit_for_binding(&graph, binding);
    let node = graph.nodes.iter().find(|node| node.id == unit).unwrap();
    serde_json::to_value(
        node.synchronous_execution
            .as_ref()
            .expect("execution report"),
    )
    .expect("serialize execution report")
}

fn has_constraining_callback_edge(
    fixture: &Fixture,
    caller_binding: &str,
    target_binding: &str,
) -> bool {
    let graph = fixture.dependency_graph();
    let caller = source_unit_for_binding(&graph, caller_binding);
    let target = source_unit_for_binding(&graph, target_binding);
    graph.edges.iter().any(|edge| {
        edge.source == caller
            && edge.target == target
            && edge.edge_kind == DepKind::EagerUse
            && edge.binding.as_deref() == Some(target_binding)
            && edge.constrains_init_order
    })
}

fn split_fixture(source: &'static str) -> FixtureOpts<'static> {
    FixtureOpts::new(
        source,
        vec![
            logical_module(
                "values/dynamic",
                &[Member::new("dynamic"), Member::new("prefix")],
            ),
            logical_module("values/later", &[Member::new("later")]),
        ],
    )
}

fn assert_split_preserved(source: &'static str) {
    let fixture = run_fixture(split_fixture(source));
    assert_preserved_entry_output(&fixture, "1\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/values/dynamic.js",
        &["dynamic"],
        &[],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/values/later.js",
        &["later"],
        &[],
    );
}

fn assert_split_rejected(opts: FixtureOpts<'_>) {
    expect_atomic_conflict_rejection(opts, &["values/dynamic", "values/later"], &[]);
}

fn assert_split_rejected_with_peer(
    source: &'static str,
    peer: &'static str,
    admission_overrides: &'static [&'static str],
) {
    let peer_chunks = [("static/peer", peer)];
    assert_split_rejected(
        split_fixture(source)
            .with_extra_chunks(&peer_chunks)
            .with_admission_overrides(admission_overrides),
    );
}

#[test]
fn forward_export_and_late_const_arrow_do_not_reach_an_earlier_unknown_call() {
    assert_split_preserved(
        r#"export { readLater };
const prefix = new globalThis.Date();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const readLater = () => later;
const later = dynamic;
console.log(later);
"#,
    );
}

#[test]
fn late_exported_class_does_not_reach_an_earlier_unknown_call() {
    assert_split_preserved(
        r#"export { Reader };
const prefix = new globalThis.Date();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
class Reader { read() { return later; } }
const later = dynamic;
console.log(later);
"#,
    );
}

#[test]
fn normal_sequel_reads_the_stored_value_after_an_incomplete_initializer() {
    let fixture = run_fixture(split_fixture(
        r#"const prefix = new globalThis.Date();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
console.log(later);
"#,
    ));
    assert_preserved_entry_output(&fixture, "1\n");
    assert_eq!(
        binding_execution_report(&fixture, "dynamic")["complete"],
        false
    );
    assert_eq!(
        binding_execution_report(&fixture, "later")["complete"],
        true
    );
}

#[test]
fn throw_only_initializer_does_not_supply_a_normal_stored_value() {
    let fixture = run_fixture(FixtureOpts::new(
        "const dynamic = (() => { throw new TypeError('stopped'); })(); const later = dynamic; console.log(later);\n",
        vec![],
    ));
    assert_preserved_entry_error(&fixture, "TypeError");
    let later = binding_execution_report(&fixture, "later");
    assert_ne!(
        later["binding_effect_authority"]["authority"], "operation_receipt",
        "a throw-only initializer cannot certify its sequel: {later:#}"
    );
}

#[test]
fn exported_hoisted_function_is_reachable_before_its_declaration() {
    assert_split_rejected_with_peer(
        r#"import "./peer.js";
export function readLater() { return later; }
const prefix = new globalThis.Date();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
console.log(later);
"#,
        r#"import { readLater } from "./app.js";
readLater();
export {};
"#,
        &[],
    );
}

#[test]
fn exported_default_function_is_reachable_before_its_declaration() {
    assert_split_rejected_with_peer(
        r#"import "./peer.js";
const prefix = new globalThis.Date();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
export default function () { return later; }
console.log(later);
"#,
        r#"import readLater from "./app.js";
readLater();
export {};
"#,
        &[],
    );
}

#[test]
fn initialized_exported_const_arrow_reaches_a_later_unknown_call() {
    assert_split_rejected_with_peer(
        r#"import "./peer.js";
export const readLater = () => later;
const prefix = new globalThis.Date();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
console.log(later);
"#,
        r#"import { readLater } from "./app.js";
readLater();
export {};
"#,
        &[],
    );
}

#[test]
fn reachable_opaque_exported_body_retains_lexical_callback_reads() {
    assert_split_rejected_with_peer(
        r#"import "./peer.js";
export function opaque() { foreignTag`${readLater}`; }
const prefix = new globalThis.Date();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
function readLater() { return later; }
console.log(later);
"#,
        r#"import { opaque } from "./app.js";
opaque();
export {};
"#,
        &[],
    );
}

#[test]
fn reachable_exported_direct_eval_retains_all_local_callables() {
    assert_split_rejected_with_peer(
        r#"import "./peer.js";
export function evaluate() { eval("readLater()"); }
const prefix = new globalThis.Date();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
function readLater() { return later; }
console.log(later);
"#,
        r#"import { evaluate } from "./app.js";
evaluate();
export {};
"#,
        &["a1_eval"],
    );
}

#[test]
fn opaque_export_without_private_capture_does_not_widen_to_all_source_units() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"export function opaque(callback) { callback?.(); }
const prefix = new globalThis.Date();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
function readLater() { return later; }
console.log(later);
"#,
        vec![
            logical_module(
                "values/dynamic",
                &[Member::new("dynamic"), Member::new("prefix")],
            ),
            logical_module(
                "values/later",
                &[Member::new("later"), Member::new("readLater")],
            ),
        ],
    ));

    assert_preserved_entry_output(&fixture, "1\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/values/dynamic.js",
        &["dynamic"],
        &[],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/values/later.js",
        &["later"],
        &[],
    );
}

#[test]
fn terminal_call_completion_preserves_abrupt_and_callback_reach_boundaries() {
    let throw_only = run_fixture(FixtureOpts::new(
        r#"function stop() { throw new TypeError("stopped"); }
const dynamic = stop();
const sequel = dynamic(() => later);
const later = 1;
console.log(sequel);
"#,
        vec![logical_module(
            "all",
            &[
                Member::new("stop"),
                Member::new("dynamic"),
                Member::new("sequel"),
                Member::new("later"),
            ],
        )],
    ));
    assert_preserved_entry_error(&throw_only, "stopped");
    assert!(
        !has_constraining_callback_edge(&throw_only, "sequel", "later"),
        "a call after the throw-only initializer must not acquire callback reach"
    );

    let normal_return = run_fixture(FixtureOpts::new(
        r#"function returnCallback() { return callback => callback(); }
const dynamic = returnCallback();
const sequel = dynamic(() => later);
const later = 1;
console.log(sequel);
"#,
        vec![logical_module(
            "all",
            &[
                Member::new("returnCallback"),
                Member::new("dynamic"),
                Member::new("sequel"),
                Member::new("later"),
            ],
        )],
    ));
    assert_preserved_entry_error(
        &normal_return,
        "Cannot access 'later' before initialization",
    );
    assert!(
        has_constraining_callback_edge(&normal_return, "sequel", "later"),
        "a normal returned callable must retain callback reach at its later invocation"
    );

    let incomplete_throw_only = run_fixture(FixtureOpts::new(
        r#"function stopAfterCallback(callback) {
  callback();
  foreignTag`unreached`;
  throw new TypeError("stopped after callback");
}
const dynamic = stopAfterCallback(() => later);
const later = 1;
console.log(dynamic);
"#,
        vec![logical_module(
            "all",
            &[
                Member::new("stopAfterCallback"),
                Member::new("dynamic"),
                Member::new("later"),
            ],
        )],
    ));
    assert_preserved_entry_error(
        &incomplete_throw_only,
        "Cannot access 'later' before initialization",
    );
    assert!(
        has_constraining_callback_edge(&incomplete_throw_only, "dynamic", "later"),
        "an incomplete no-normal-return body must keep callback effects reached before throw"
    );
}
