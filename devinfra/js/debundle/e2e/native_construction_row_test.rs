//! `Error` construction runs no user code once its message converts silently,
//! so a published callback cannot reenter through it.

use debundle_e2e_graph_support::FixtureGraphExt;
use debundle_e2e_support::*;

fn opts<'a>(source: &'a str, anonymous: &[&str]) -> FixtureOpts<'a> {
    FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon("values/failure", &[Member::new("failure")], anonymous),
            logical_module("values/later", &[Member::new("later")]),
        ],
    )
}

fn split_error_opts<'a>(source: &'a str, mutations: &[&str]) -> FixtureOpts<'a> {
    FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "values/later",
                &[Member::new("later")],
                &["globalThis.saved = () => later;"],
            ),
            logical_module_with_anon("values/failure", &[Member::new("failure")], mutations),
        ],
    )
}

fn colocated_opts<'a>(source: &'a str, members: &[Member], anonymous: &[&str]) -> FixtureOpts<'a> {
    FixtureOpts::new(
        source,
        vec![logical_module_with_anon("values/all", members, anonymous)],
    )
}

fn expect_schedule_refusal(opts: FixtureOpts<'_>, source_output: &str) {
    let rejected = run_rejection_fixture_with_graphs_and_args(opts, &["--emit-on-rejection"], 3);
    assert!(
        rejected
            .stderr
            .contains("evaluation_schedule_unknown_reordered"),
        "expected a conservative evaluation-schedule refusal:\n{}",
        rejected.stderr
    );
    assert!(rejected.gate_rejected_marker().is_file());
    assert_node_output(
        &rejected.source_root.join("static/app.js"),
        source_output,
        "",
    );
}

fn source(mutations: &[&str], declaration: &str) -> String {
    format!(
        "globalThis.saved = () => later;\n{}\n{declaration}\nconst later = 42;\nconsole.log(failure.message, later);\n",
        mutations.join("\n")
    )
}

fn anonymous<'a>(mutations: &[&'a str]) -> Vec<&'a str> {
    [&["globalThis.saved = () => later;"], mutations].concat()
}

#[test]
fn error_with_a_primitive_message_cannot_reenter() {
    for (mutations, declaration) in [
        (&[][..], "const failure = new Error(\"boom\");"),
        (&[][..], "const failure = Error(\"boom\", 0);"),
        // V8 formats the stack lazily: construction does not call it.
        (
            &["Error.prepareStackTrace = () => globalThis.saved();"][..],
            "const failure = new Error(\"boom\");",
        ),
    ] {
        let source = source(mutations, declaration);
        let fixture = run_fixture_with_graphs(colocated_opts(
            &source,
            &[Member::new("failure"), Member::new("later")],
            &anonymous(mutations),
        ));
        assert_preserved_entry_output(&fixture, "boom 42\n");
        assert_all_emitted_js_checks(&fixture);

        let split_opts = split_error_opts(&source, mutations);
        if mutations.is_empty() && declaration.starts_with("const failure = Error(") {
            // Keep the assignment and its captured cell in the same module. The
            // primitive-message Error call can move after that publication without
            // changing their source-relative setter phase.
            let split = run_fixture_with_graphs(split_opts);
            assert_preserved_entry_output(&split, "boom 42\n");
            assert_all_emitted_js_checks(&split);
            assert_module_source(
                &split.out_root,
                "static/app/modules/values/later.js",
                &["globalThis.saved = ()=>later;", "const later = 42;"],
                &["const failure = Error("],
            );
            assert_module_source(
                &split.out_root,
                "static/app/modules/values/failure.js",
                &["const failure = Error("],
                &["globalThis.saved = () => later;", "const later = 42;"],
            );
        } else {
            // The `new Error` form (and a prepareStackTrace mutation) does not
            // retain this schedule proof. Preserve the host-visible setter phase
            // with a typed schedule refusal; the colocated controls above retain
            // the supported native-row semantics.
            expect_schedule_refusal(split_opts, "boom 42\n");
        }
    }
}

#[test]
fn error_message_conversion_or_replacement_can_reenter() {
    for (mutations, declaration) in [
        (
            &[][..],
            "const failure = new Error({ toString() { try { globalThis.saved(); return \"ready\"; } catch (error) { return error.name; } } });",
        ),
        (
            &[][..],
            "const failure = new Error(\"boom\", { get cause() { try { globalThis.saved(); } catch (error) { return error.name; } } });",
        ),
        (
            &[
                "Error = function Error() { try { globalThis.saved(); return { message: \"ready\" }; } catch (error) { return { message: error.name }; } };",
            ][..],
            "const failure = new Error(\"boom\");",
        ),
    ] {
        let source = source(mutations, declaration);
        expect_atomic_conflict_rejection(
            opts(&source, &anonymous(mutations)),
            &["values/failure", "values/later"],
            &[],
        );
    }
}

/// `restore` never runs at init, but the census covers its write.
fn assign_source(restore: &str) -> String {
    format!(
        "globalThis.saved = () => later;\nfunction restore(saved) {{ {restore} }}\nconst failure = new Error(\"boom\");\nconst later = 42;\nconsole.log(failure.message, later);\n"
    )
}

fn assign_split_opts(source: &str) -> FixtureOpts<'_> {
    FixtureOpts::new(
        source,
        vec![
            logical_module(
                "values/failure",
                &[Member::new("restore"), Member::new("failure")],
            ),
            logical_module_with_anon(
                "values/later",
                &[Member::new("later")],
                &["globalThis.saved = () => later;"],
            ),
        ],
    )
}

fn assign_colocated_opts(source: &str) -> FixtureOpts<'_> {
    colocated_opts(
        source,
        &[
            Member::new("restore"),
            Member::new("failure"),
            Member::new("later"),
        ],
        &["globalThis.saved = () => later;"],
    )
}

/// The keys of the census witness for `restore`'s write to `%Error%`.
fn restore_slot_keys(fixture: &Fixture) -> Vec<serde_json::Value> {
    let graph = serde_json::to_value(fixture.program_graph()).unwrap();
    graph["native_integrity_witnesses"]
        .as_array()
        .unwrap()
        .iter()
        .flat_map(|witness| witness["target_slots"].as_array().unwrap().clone())
        .map(|slot| slot["key"].clone())
        .collect()
}

#[test]
fn object_assign_from_a_static_literal_writes_only_its_keys() {
    let source = assign_source(
        "Object.assign(Error, { prepareStackTrace: () => globalThis.saved(), stackTraceLimit: saved });",
    );
    let fixture = run_fixture_with_graphs(assign_colocated_opts(&source));
    assert_preserved_entry_output(&fixture, "boom 42\n");
    assert_all_emitted_js_checks(&fixture);
    assert_eq!(
        restore_slot_keys(&fixture),
        [
            serde_json::json!({ "string": "prepareStackTrace" }),
            serde_json::json!({ "string": "stackTraceLimit" }),
        ]
    );
    expect_schedule_refusal(assign_split_opts(&source), "boom 42\n");
}

#[test]
fn object_assign_from_another_source_may_write_any_key() {
    for restore in [
        "Object.assign(Error, saved);",
        "Object.assign(Error, { get stackTraceLimit() { return saved; } });",
        "Object.assign(Error, { ...saved });",
        "Object.assign(Error, { [saved]: 5 });",
    ] {
        let source = assign_source(restore);
        let fixture = run_fixture_with_graphs(assign_colocated_opts(&source));
        let keys = restore_slot_keys(&fixture);
        assert!(
            keys.contains(&serde_json::json!("unknown")),
            "{restore}: {keys:?}"
        );
        assert_preserved_entry_output(&fixture, "boom 42\n");
        assert_all_emitted_js_checks(&fixture);
        expect_schedule_refusal(assign_split_opts(&source), "boom 42\n");
    }
}
