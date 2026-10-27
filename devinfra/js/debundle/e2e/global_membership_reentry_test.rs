//! Explicit root/key contracts narrow host membership protocol reentry only.

use analysis::DepKind;
use debundle_e2e_support::*;

fn probe_opts<'a>(source: &'a str) -> FixtureOpts<'a> {
    let mut opts = FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "startup/probe",
                &[Member::new("supported")],
                &["globalThis.addEventListener('neutral', read);"],
            ),
            logical_module("state/value", &[Member::new("read"), Member::new("later")]),
        ],
    );
    opts.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    opts.extra_files = &[
        (
            "static/app/prelude.js",
            "globalThis.window = globalThis; globalThis.addEventListener = () => {}; export {};\n",
        ),
        (
            "static/prelude.js",
            "globalThis.window = globalThis; globalThis.addEventListener = () => {}; export {};\n",
        ),
    ];
    opts
}

const SOURCE: &str = "import './prelude.js';\nfunction read() { return later; }\nglobalThis.addEventListener('neutral', read);\nconst supported = 'feature' in globalThis;\nconst later = supported ? 42 : 42;\nconsole.log(read());\n";

#[test]
fn listed_host_membership_can_preserve_deferred_callback_initialization() {
    for root in ["globalThis", "window"] {
        let source = SOURCE.replace("in globalThis;", &format!("in {root};"));
        let mut opts = probe_opts(&source);
        let assertions = [(root, "feature")];
        opts.non_reentrant_global_membership = &assertions;
        let fixture = run_fixture(opts);
        assert_preserved_entry_output(&fixture, "42\n");
        assert_probe_read(&fixture, false);
        let graph = fixture.dependency_graph();
        let probe = source_unit_for_binding(&graph, "supported");
        assert!(matches!(
            &graph
                .nodes
                .iter()
                .find(|node| node.id == probe)
                .unwrap()
                .purity,
            analysis::Purity::NotPure { .. }
        ));
        assert_all_emitted_js_checks(&fixture);
    }
}

#[test]
fn unasserted_host_membership_keeps_callback_reentry() {
    expect_atomic_conflict_rejection(probe_opts(SOURCE), &["startup/probe", "state/value"], &[]);
}

fn assert_probe_read(fixture: &Fixture, eager: bool) {
    let graph = fixture.dependency_graph();
    let source = source_unit_for_binding(&graph, "supported");
    let target = source_unit_for_binding(&graph, "later");
    assert_eq!(
        graph.edges.iter().any(|edge| edge.source == source
            && edge.target == target
            && edge.edge_kind == DepKind::EagerUse),
        eager
    );
}

fn assert_conservative_probe(
    source: &str,
    extra: &[&'static str],
    statements: &[&str],
    expected: &str,
) {
    let mut callbacks: Vec<_> = extra.iter().map(|name| Member::new(name)).collect();
    callbacks.extend([Member::new("read"), Member::new("supported")]);
    let mut colocated_members: Vec<_> = extra.iter().map(|name| Member::new(name)).collect();
    colocated_members.extend([Member::new("read"), Member::new("supported")]);
    colocated_members.push(Member::new("later"));
    let mut all_statements = statements.to_vec();
    all_statements.extend(["globalThis.inspectState = read;", "later = 42;"]);
    let assertions = [
        ("globalThis", "feature"),
        ("window", "feature"),
        ("globalThis", "\u{fffd}"),
    ];
    let mut colocated = FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "all",
            &colocated_members,
            &all_statements,
        )],
    );
    colocated.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    colocated.non_reentrant_global_membership = &assertions;
    let fixture = run_fixture(colocated);
    assert_preserved_entry_output(&fixture, expected);
    assert_probe_read(&fixture, true);
    let mut callback_statements = statements.to_vec();
    callback_statements.push("globalThis.inspectState = read;");
    let mut split = FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon("startup/probe", &callbacks, &callback_statements),
            logical_module_with_anon("state/value", &[Member::new("later")], &["later = 42;"]),
        ],
    );
    split.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    split.non_reentrant_global_membership = &assertions;
    expect_atomic_conflict_rejection(split, &["startup/probe", "state/value"], &[]);
}

#[test]
fn unlisted_keys_aliases_and_effectful_receivers_keep_fallback() {
    for (setup, expression, bindings, expected) in [
        ("", "'other' in globalThis", vec![], "42\n"),
        ("", "'\\ud800' in globalThis", vec![], "42\n"),
        ("", "`feature` in globalThis", vec![], "42\n"),
        ("", "'feature' in global", vec![], "42\n"),
        ("", "'feature' in globalThis.globalThis", vec![], "42\n"),
        (
            "const root = globalThis;",
            "'feature' in root",
            vec!["root"],
            "42\n",
        ),
        (
            "function receiver() { console.log(read()); return globalThis; }",
            "'feature' in receiver()",
            vec!["receiver"],
            "0\n42\n",
        ),
    ] {
        let source = format!(
            "function read() {{ return later; }}\nlet later = 0;\nglobalThis.inspectState = read;\n{setup}\nconst supported = {expression};\nlater = 42;\nconsole.log(later);\n"
        );
        assert_conservative_probe(&source, &bindings, &[], expected);
    }
}

#[test]
fn coercing_key_keeps_its_synchronous_effects() {
    let source = "function read() { return later; }\nlet later = 0;\nglobalThis.inspectState = read;\nconst key = { [Symbol.toPrimitive]() { console.log(read()); return 'feature'; } };\nconst supported = key in globalThis;\nlater = 42;\nconsole.log(later);\n";
    assert_conservative_probe(source, &["key"], &[], "0\n42\n");
}

#[test]
fn shadowed_builtin_receiver_and_proxy_keep_fallback() {
    let source = "function read() { return later; }\nlet later = 0;\nglobalThis.inspectState = read;\nconst window = new Proxy({}, { has() { console.log(read()); return false; } });\nconst supported = 'feature' in window;\nlater = 42;\nconsole.log(later);\n";
    assert_conservative_probe(source, &["window"], &[], "0\n42\n");
}

#[test]
fn scoped_parameter_cannot_acquire_builtin_identity_from_its_name() {
    let source = "function read() { return later; }\nlet later = 0;\nglobalThis.inspectState = read;\nfunction probe(globalThis) { return 'feature' in globalThis; }\nconst receiver = new Proxy({}, { has() { console.log(read()); return false; } });\nconst supported = probe(receiver);\nlater = 42;\nconsole.log(later);\n";
    assert_conservative_probe(source, &["probe", "receiver"], &[], "0\n42\n");
}

#[test]
fn recognized_root_replacement_and_prototype_mutation_keep_fallback() {
    for (setup, expression) in [
        (
            "globalThis.window = new Proxy({}, { has() { console.log(read()); return false; } });",
            "'feature' in window",
        ),
        (
            "Object.setPrototypeOf(globalThis, new Proxy({}, { has() { console.log(read()); return false; } }));",
            "'feature' in globalThis",
        ),
        (
            "globalThis.__proto__ = new Proxy({}, { has() { console.log(read()); return false; } });",
            "'feature' in globalThis",
        ),
    ] {
        let source = format!(
            "function read() {{ return later; }}\nlet later = 0;\nglobalThis.inspectState = read;\n{setup}\nconst supported = {expression};\nlater = 42;\nconsole.log(later);\n"
        );
        assert_conservative_probe(&source, &[], &[setup], "0\n42\n");
    }
}

#[test]
fn certified_protocol_does_not_remove_receiver_reference_errors() {
    let source = "let supported = false;\ntry { supported = 'feature' in window; } catch (error) { console.log(error.name); }\nconsole.log(supported);\n";
    let mut opts = FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "probe",
            &[Member::new("supported")],
            &[
                "try { supported = 'feature' in window; } catch (error) { console.log(error.name); }",
            ],
        )],
    );
    opts.non_reentrant_global_membership = &[("window", "feature")];
    let fixture = run_fixture(opts);
    assert_preserved_entry_output(&fixture, "ReferenceError\nfalse\n");
}

#[test]
fn confined_earlier_alias_does_not_publish_or_modify_probe_surface() {
    let prefix = "(function () { const root = globalThis; root.neutralCache = root.neutralCache || {}; root.neutralCache.marker = 'ready'; })();";
    let source = format!("{prefix}\n{SOURCE}");
    let mut opts = probe_opts(&source);
    opts.logical_modules[0] = logical_module_with_anon(
        "startup/probe",
        &[Member::new("supported")],
        &[prefix, "globalThis.addEventListener('neutral', read);"],
    );
    opts.non_reentrant_global_membership = &[("globalThis", "feature")];
    let fixture = run_fixture(opts);
    assert_preserved_entry_output(&fixture, "42\n");
    assert_probe_read(&fixture, false);
}

#[test]
fn deferred_publication_assertion_does_not_certify_membership_reentry() {
    let mut opts = probe_opts(SOURCE);
    opts.deferred_global_publications = &[("globalThis", "unusedSlot")];
    expect_atomic_conflict_rejection(opts, &["startup/probe", "state/value"], &[]);
}
