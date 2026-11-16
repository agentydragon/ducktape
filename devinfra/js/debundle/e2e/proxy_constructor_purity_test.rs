//! Native `Proxy` construction stores handlers without invoking their traps.

use debundle_e2e_support::{
    FixtureOpts, Member, assert_entry_output, assert_node_output, assert_preserved_entry_output,
    expect_cycle_rejection, logical_module, read_json, run_emit_on_rejection_fixture, run_fixture,
};

#[test]
fn fresh_proxy_constructor_args_have_no_graph_cycle_but_keep_unknown_completion() {
    let source = r#"const a = new Proxy({ value: 1 }, {});
const b = new Proxy({ value: 2 }, {});
const c = new Proxy({ value: 3 }, {});
console.log(a.value, b.value, c.value);
export { a, b, c };
"#;
    let rejected = run_emit_on_rejection_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module("mod_a", &[Member::new("a"), Member::new("c")]),
            logical_module("mod_b", &[Member::new("b")]),
        ],
    ));

    // The graph's native timing proof does not grant an ordered Proxy
    // construction receipt. Keep that separate completion limit explicit.
    let report: serde_json::Value =
        read_json(&rejected.report_root.join("evaluation_schedule.json"));
    assert_eq!(report["kind"], "evaluation_schedule_refused");
    assert!(
        report["rejection"]["reason"]
            .as_str()
            .unwrap()
            .starts_with("evaluation_schedule_unknown_reordered")
    );
    assert_eq!(
        rejected.candidate_relocation_assessment()["legacy_gate_accepts"],
        true
    );
    assert!(rejected.gate_rejected_marker().exists());
    assert_node_output(&rejected.source_root.join("static/app.js"), "1 2 3\n", "");
    assert_node_output(&rejected.entry_path(), "1 2 3\n", "");
    let colocated = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module(
            "proxies",
            &[Member::new("a"), Member::new("b"), Member::new("c")],
        )],
    ));
    assert_preserved_entry_output(&colocated, "1 2 3\n");
}

#[test]
fn proxy_traps_run_only_after_property_access() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = new Proxy({ value: 1 }, { get(target, key) { console.log("trap", key); return target[key]; } });
const b = new Proxy({ value: 2 }, { get(target, key) { console.log("trap", key); return target[key]; } });
const c = new Proxy({ value: 3 }, { get(target, key) { console.log("trap", key); return target[key]; } });
console.log(a.value, b.value, c.value);
export { a, b, c };
"#,
        vec![logical_module(
            "mod_a",
            &[Member::new("a"), Member::new("b"), Member::new("c")],
        )],
    ));

    assert_entry_output(&fixture, "trap value\ntrap value\ntrap value\n1 2 3\n");
}

#[test]
fn effectful_nested_method_keys_block_proxy_module_split() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const a = new Proxy({ nested: { [console.log("a")]() {} } }, {});
const b = new Proxy({ nested: { [console.log("b")]() {} } }, {});
const c = new Proxy({ nested: { [console.log("c")]() {} } }, {});
console.log("done");
export { a, b, c };
"#,
            vec![
                logical_module("mod_a", &[Member::new("a"), Member::new("c")]),
                logical_module("mod_b", &[Member::new("b")]),
            ],
        ),
        &["mod_a", "mod_b"],
    );
}

#[test]
fn effectful_nested_method_keys_block_object_literal_module_split() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const a = { nested: { [console.log("a")]() {} } };
const b = { nested: { [console.log("b")]() {} } };
const c = { nested: { [console.log("c")]() {} } };
console.log("done");
export { a, b, c };
"#,
            vec![
                logical_module("mod_a", &[Member::new("a"), Member::new("c")]),
                logical_module("mod_b", &[Member::new("b")]),
            ],
        ),
        &["mod_a", "mod_b"],
    );
}
