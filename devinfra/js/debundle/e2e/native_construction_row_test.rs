//! `Error` construction runs no user code once its message converts silently,
//! so a published callback cannot reenter through it.

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
        let fixture = run_fixture(opts(&source, &anonymous(mutations)));
        assert_preserved_entry_output(&fixture, "boom 42\n");
        assert_all_emitted_js_checks(&fixture);
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

fn assign_opts(source: &str) -> FixtureOpts<'_> {
    FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "values/failure",
                &[Member::new("restore"), Member::new("failure")],
                &["globalThis.saved = () => later;"],
            ),
            logical_module("values/later", &[Member::new("later")]),
        ],
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
    let fixture = run_fixture(assign_opts(&source));
    assert_preserved_entry_output(&fixture, "boom 42\n");
    assert_eq!(
        restore_slot_keys(&fixture),
        [
            serde_json::json!({ "string": "prepareStackTrace" }),
            serde_json::json!({ "string": "stackTraceLimit" }),
        ]
    );
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
        let fixture = run_fixture(assign_opts(&source));
        let keys = restore_slot_keys(&fixture);
        assert!(
            keys.contains(&serde_json::json!("unknown")),
            "{restore}: {keys:?}"
        );
    }
}
