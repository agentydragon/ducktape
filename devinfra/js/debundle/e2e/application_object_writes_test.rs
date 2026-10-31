//! A census write the author audited as targeting an object the application
//! creates reaches no intrinsic slot.

use debundle_e2e_support::*;

/// zod's shape: a descriptor installed on the prototype of an instance, under
/// a caller-supplied key, so the census sees an unknown receiver and key.
const INSTALL: &str = "Object.defineProperty(Object.getPrototypeOf(instance), name, { value })";

fn source(extra: &str) -> String {
    format!(
        "globalThis.saved = () => later;\nfunction install(instance, name, value) {{ {INSTALL}; }}\n{extra}const collection = new Map();\nconst later = 42;\nconsole.log(collection.size, later);\n"
    )
}

fn opts<'a>(source: &'a str, members: &[&'static str], writes: &[&str]) -> FixtureOpts<'a> {
    let mut native: Vec<Member> = members.iter().map(|member| Member::new(member)).collect();
    native.push(Member::new("collection"));
    FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "values/native",
                &native,
                &["globalThis.saved = () => later;"],
            ),
            logical_module("values/later", &[Member::new("later")]),
        ],
    )
    .with_application_object_writes(
        "static/app",
        writes
            .iter()
            .map(|text| ApplicationObjectWriteContract {
                source_text: (*text).to_owned(),
                note: Some("The receiver is a prototype the application creates.".into()),
            })
            .collect(),
    )
}

#[test]
fn a_certified_application_object_write_reaches_no_intrinsic() {
    let source = source("");
    let fixture = run_fixture(opts(&source, &["install"], &[INSTALL]));
    assert_preserved_entry_output(&fixture, "0 42\n");
    assert_all_emitted_js_checks(&fixture);
    let graph = fixture.dependency_graph();
    let node_id = source_unit_for_binding(&graph, "collection");
    let node = graph.nodes.iter().find(|node| node.id == node_id).unwrap();
    let report = serde_json::to_value(node).unwrap();
    assert_eq!(
        report["synchronous_execution"]["native_integrity_uses"][0]["application_object_write_refs"],
        serde_json::json!([{ "defining_chunk": "static/app", "input_index": 0 }]),
        "{report}"
    );
}

#[test]
fn without_the_premise_the_write_may_replace_map() {
    let source = source("");
    expect_atomic_conflict_rejection(
        opts(&source, &["install"], &[]),
        &["values/native", "values/later"],
        &[],
    );
}

#[test]
fn a_premise_on_an_intrinsic_or_no_write_is_rejected() {
    const PATCH: &str = "Object.defineProperty(Array.prototype, name, { value })";
    let source = source(&format!("function patch(name, value) {{ {PATCH}; }}\n"));
    expect_rejection_containing_all(
        opts(&source, &["install", "patch"], &[INSTALL, PATCH]),
        &["application_object_writes[1]", "may be"],
    );
    let source = self::source("");
    expect_rejection_containing_all(
        opts(&source, &["install"], &["const collection = new Map();"]),
        &[
            "application_object_writes[0]",
            "covers no native-census write",
        ],
    );
}

#[test]
fn a_selector_must_occur_exactly_once() {
    let source = source("");
    for (text, count) in [("value", "found 2"), ("absent text", "found 0")] {
        expect_rejection_containing_all(
            opts(&source, &["install"], &[text]),
            &["application_object_writes[0]", "exactly once", count],
        );
    }
}

/// A bundle-owned registry beside the certified write: without the premise its
/// unknown key on an unknown receiver drops every bundle-owned global.
const REGISTRY: &str = "const root = globalThis;\nfunction getInstance(name) { root.instances || (root.instances = {}); root.instances[name] || (root.instances[name] = { name }); return root.instances[name]; }\n";

#[test]
fn a_certified_write_keeps_bundle_owned_globals() {
    let source = source(REGISTRY);
    let members = ["install", "root", "getInstance"];
    for (writes, accepted) in [(&[INSTALL][..], true), (&[][..], false)] {
        let mut opts = opts(&source, &members, writes);
        opts.host_behavior.predefined_globals = PredefinedGlobals::Standard;
        if accepted {
            assert_preserved_entry_output(&run_fixture(opts), "0 42\n");
        } else {
            expect_atomic_conflict_rejection(opts, &["values/native", "values/later"], &[]);
        }
    }
}
