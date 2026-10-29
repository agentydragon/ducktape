use debundle_e2e_support::{
    TreeFixture, TreeRun, assert_module_source, assert_node_output, run_tree_fixture,
};

const PROVIDER: &str = "const callbacks = new Set();\nexport function register(name, callback) { console.log('registered', name); callbacks.add(callback); }\nexport function fire() { console.log('fired'); for (const callback of callbacks) callback(); }\n";

fn run_registration_fixture(main: &str, registration_module: &str) -> TreeRun {
    let run = run_tree_fixture(
        &TreeFixture {
            chunks: &[("main", main), ("peer", PROVIDER)],
            module_roots: &[("app/main", "main"), ("app/peer", "peer")],
            modules: &[
                ("app/main/callbacks/registration.yaml", registration_module),
                (
                    "app/main/values/later.yaml",
                    "members:\n  - name: later\n    selector: {binding: {name: later}}\n",
                ),
            ],
        },
        &[],
    );
    std::fs::write(
        run.root().join("snapshot/package.json"),
        r#"{"type":"module"}"#,
    )
    .unwrap();
    run
}

#[test]
fn anonymous_registration_call_without_contract_is_rejected() {
    let run = run_registration_fixture(
        "import * as peer from './peer.js';\nfunction callback() { later.count += 1; }\npeer.register('topic', callback);\nconst later = { count: 0 };\npeer.fire();\nconsole.log(later.count);\n",
        "members:\n  - name: callback\n    selector: {binding: {name: callback}}\nanonymous_statements:\n  - match: \"peer.register('topic', callback);\"\n",
    );
    assert!(
        !run.result.status.success(),
        "debundler unexpectedly accepted the unannotated registration call\nstdout:\n{}\nstderr:\n{}",
        run.result.stdout,
        run.result.stderr,
    );
    assert!(
        run.result.stderr.contains("EagerUse cycle"),
        "expected the baseline EagerUse rejection, got:\n{}",
        run.result.stderr
    );
}

#[test]
fn annotated_registration_preserves_call_effects_and_retains_callback() {
    let run = run_registration_fixture(
        "import * as peer from './peer.js';\npeer.register('topic', () => { later.count += 1; });\nconst later = { count: 0 };\npeer.fire();\nconsole.log(later.count);\n",
        "anonymous_statements:\n  - match: \"peer.register('topic', () => { later.count += 1; });\"\n    no_reentry_call: {callback_arguments: [1]}\n",
    );
    assert!(
        run.result.status.success(),
        "annotated registration should permit the proven split\nstdout:\n{}\nstderr:\n{}",
        run.result.stdout,
        run.result.stderr,
    );
    assert_module_source(
        &run.out_root,
        "app/main/callbacks/registration.js",
        &["peer.register('topic', ()=>", "later.count += 1"],
        &["no_reentry_call"],
    );
    assert_node_output(
        &run.root().join("snapshot/main.js"),
        "registered topic\nfired\n1\n",
        "",
    );
    assert_node_output(
        &run.out_root.join("app/main/entry.js"),
        "registered topic\nfired\n1\n",
        "",
    );
}

#[test]
fn later_opaque_call_keeps_tdz_callback_rejection() {
    let run = run_registration_fixture(
        "import * as peer from './peer.js';\npeer.register('topic', () => { later.count += 1; });\npeer.fire();\nconst later = { count: 0 };\nconsole.log(later.count);\n",
        "anonymous_statements:\n  - match: \"peer.register('topic', () => { later.count += 1; });\"\n    no_reentry_call: {callback_arguments: [1]}\n",
    );
    assert!(
        !run.result.status.success(),
        "a later opaque call before the lexical initializer must remain rejected\nstdout:\n{}\nstderr:\n{}",
        run.result.stdout,
        run.result.stderr,
    );
    assert!(
        run.result.stderr.contains("EagerUse cycle"),
        "expected the retained callback's TDZ read to constrain emission, got:\n{}",
        run.result.stderr
    );
}

#[test]
fn guarded_registration_preserves_condition_effects_and_callback_lifetime() {
    for (guard, expected) in [
        (
            "(console.log('guard'), typeof console !== 'undefined')",
            "guard\nregistered topic\nfired\n1\n",
        ),
        (
            "true && (console.log('guard'), typeof missingGlobal !== 'undefined')",
            "guard\nfired\n0\n",
        ),
    ] {
        let statement =
            format!("{guard} && peer.register('topic', () => {{ later.count += 1; }});");
        let main = format!(
            "import * as peer from './peer.js';\n{statement}\nconst later = {{ count: 0 }};\npeer.fire();\nconsole.log(later.count);\n"
        );
        let registration_module = format!(
            "anonymous_statements:\n  - match: \"{statement}\"\n    no_reentry_call: {{callback_arguments: [1]}}\n"
        );
        let run = run_registration_fixture(&main, &registration_module);
        assert!(
            run.result.status.success(),
            "guarded registration should permit the proven split\nstdout:\n{}\nstderr:\n{}",
            run.result.stdout,
            run.result.stderr,
        );
        assert_node_output(&run.root().join("snapshot/main.js"), expected, "");
        assert_node_output(&run.out_root.join("app/main/entry.js"), expected, "");
    }
}

#[test]
fn guarded_registration_keeps_guard_and_later_call_tdz_rejections() {
    for (guard, before_initializer) in [
        ("later.count === 0", ""),
        ("(() => later.count === 0)()", ""),
        ("true", "peer.fire();"),
    ] {
        let statement =
            format!("{guard} && peer.register('topic', () => {{ later.count += 1; }});");
        let main = format!(
            "import * as peer from './peer.js';\n{statement}\n{before_initializer}\nconst later = {{ count: 0 }};\npeer.fire();\n"
        );
        let registration_module = format!(
            "anonymous_statements:\n  - match: \"{statement}\"\n    no_reentry_call: {{callback_arguments: [1]}}\n"
        );
        let run = run_registration_fixture(&main, &registration_module);
        assert!(
            !run.result.status.success(),
            "guard and later opaque-call effects must remain eager\nstdout:\n{}\nstderr:\n{}",
            run.result.stdout,
            run.result.stderr,
        );
        assert!(
            run.result.stderr.contains("EagerUse cycle"),
            "expected a TDZ rejection, got:\n{}",
            run.result.stderr
        );
    }
}

#[test]
fn registration_signature_errors_are_reported() {
    let duplicate = run_registration_fixture(
        "import * as peer from './peer.js';\nfunction callback() { later.count += 1; }\npeer.register('topic', callback);\nconst later = { count: 0 };\npeer.fire();\n",
        "members:\n  - name: callback\n    selector: {binding: {name: callback}}\nanonymous_statements:\n  - match: \"peer.register('topic', callback);\"\n    no_reentry_call: {callback_arguments: [1, 1]}\n",
    );
    assert!(
        !duplicate.result.status.success(),
        "duplicate callback positions unexpectedly accepted"
    );
    assert!(
        duplicate
            .result
            .stderr
            .contains("duplicate no_reentry_call callback argument position 1"),
        "{}",
        duplicate.result.stderr
    );

    let out_of_range = run_registration_fixture(
        "import * as peer from './peer.js';\nfunction callback() { later.count += 1; }\npeer.register('topic', callback);\nconst later = { count: 0 };\npeer.fire();\n",
        "members:\n  - name: callback\n    selector: {binding: {name: callback}}\nanonymous_statements:\n  - match: \"peer.register('topic', callback);\"\n    no_reentry_call: {callback_arguments: [2]}\n",
    );
    assert!(
        !out_of_range.result.status.success(),
        "out-of-range callback position unexpectedly accepted"
    );
    assert!(
        out_of_range
            .result
            .stderr
            .contains("no_reentry_call callback argument position 2 is out of range"),
        "{}",
        out_of_range.result.stderr
    );
}

#[test]
fn unsupported_registration_shapes_keep_conservative_rejection() {
    let cases = [
        (
            "import * as peer from './peer.js';\nfunction callback() { later.count += 1; }\npeer.register('topic', ...[callback]);\nconst later = { count: 0 };\npeer.fire();\n",
            "peer.register('topic', ...[callback]);",
            "spread argument",
        ),
        (
            "import * as peer from './peer.js';\nfunction callback() { later.count += 1; }\nfunction makeCallback() { return callback; }\npeer.register('topic', makeCallback());\nconst later = { count: 0 };\npeer.fire();\n",
            "peer.register('topic', makeCallback());",
            "nested eager callback expression",
        ),
        (
            "import * as peer from './peer.js';\nfunction callback() { later.count += 1; }\nconst method = 'register';\npeer[method]('topic', callback);\nconst later = { count: 0 };\npeer.fire();\n",
            "peer[method]('topic', callback);",
            "computed callee",
        ),
        (
            "import * as peer from './peer.js';\nfunction callback() { later.count += 1; }\nfunction getPeer() { return peer; }\ngetPeer().register('topic', callback);\nconst later = { count: 0 };\npeer.fire();\n",
            "getPeer().register('topic', callback);",
            "effectful callee receiver",
        ),
        (
            "import * as peer from './peer.js';\nfunction callback() { later.count += 1; }\npeer.register?.('topic', callback);\nconst later = { count: 0 };\npeer.fire();\n",
            "peer.register?.('topic', callback);",
            "optional callee",
        ),
    ];
    for (main, statement, shape) in cases {
        let registration_module = format!(
            "members:\n  - name: callback\n    selector: {{binding: {{name: callback}}}}\nanonymous_statements:\n  - match: \"{statement}\"\n    no_reentry_call: {{callback_arguments: [1]}}\n"
        );
        let run = run_registration_fixture(main, &registration_module);
        assert!(
            !run.result.status.success(),
            "unsupported {shape} unexpectedly received the no-reentry premise\nstdout:\n{}\nstderr:\n{}",
            run.result.stdout,
            run.result.stderr,
        );
    }
}
