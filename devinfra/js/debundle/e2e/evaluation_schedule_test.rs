//! Final emitted imports, not the chunk-local quotient, determine ESM DFS.
use debundle_e2e_support::*;

#[test]
fn a_root_primitive_operator_keeps_its_completion_proof_across_a_split() {
    let peers = [("static/peer", "export const value = 1;")];
    let opts = || {
        FixtureOpts::new(
            "import { value } from './peer.js';\nconst first = value + 1;\nconst second = 2;\nconst tail = second;\nconsole.log(first, tail);\nexport { first, second, tail };\n",
            vec![
                logical_module("outer", &[Member::new("first"), Member::new("tail")]),
                logical_module("inner", &[Member::new("second")]),
            ],
        )
        .with_extra_chunks(&peers)
    };
    let fixture = run_fixture(opts());
    assert_preserved_entry_output(&fixture, "2 2\n");
    saved_analysis::assert_staged_run_matches(opts(), &[]);
}

#[test]
fn a_missing_dependency_of_a_reexported_constant_refuses_the_split() {
    let chunks = [
        ("static/bridge", "export { limit } from './provider.js';"),
        (
            "static/provider",
            "import './missing.js'; export const limit = 2;",
        ),
    ];
    let rejected = run_rejection_fixture(
        FixtureOpts::new(
            "import { limit } from './bridge.js'; const size = 4 * limit + 256; console.log(size);",
            vec![logical_module("size", &[Member::new("size")])],
        )
        .with_extra_chunks(&chunks),
    );
    assert!(
        rejected.stderr.contains("evaluation_schedule_missing"),
        "{}",
        rejected.stderr
    );
    let report: serde_json::Value =
        read_json(&rejected.report_root.join("evaluation_schedule.json"));
    assert_eq!(report["kind"], "evaluation_schedule_refused");
    assert_eq!(report["rejection"]["source_entry"], "static/app");
    assert_node_error(
        &rejected.source_root.join("static/app.js"),
        "ERR_MODULE_NOT_FOUND",
    );
    assert!(
        !rejected.entry_path().exists(),
        "a refused schedule must not publish accepted output"
    );
}

#[test]
fn an_unchanged_external_request_keeps_the_same_loader_barrier() {
    let fixture = ProgramPlacement::new(
        "import { basename } from 'node:path'; const name = basename('/tmp/example'); console.log(name);",
    ).place_in_entry(logical_module("name", &[Member::new("name")])).run();
    assert_preserved_entry_output(&fixture, "example\n");
}

#[test]
fn a_source_throw_and_its_later_side_effect_remain_unreachable_after_splitting() {
    let fixture = ProgramPlacement::new(
        "import './provider.js'; const value = 7; console.log('unreachable', value);",
    )
    .place_in_entry(logical_module("value", &[Member::new("value")]))
    .add_chunk(
        "static/provider",
        "function stop() { throw 'stopped'; } stop(); console.log('also unreachable');",
        vec![],
    )
    .run();
    assert_preserved_entry_error(&fixture, "stopped");
}

#[test]
fn moving_an_initializer_before_an_uncaught_throw_refuses_the_changed_frontier() {
    let rejected = run_rejection_fixture(FixtureOpts::new(
        "function stop() { throw 'stopped'; } stop(); const later = 7; console.log(later);",
        vec![logical_module("later", &[Member::new("later")])],
    ));
    assert!(
        rejected
            .stderr
            .contains("evaluation_schedule_abrupt_frontier_changed"),
        "{}",
        rejected.stderr
    );
    assert_node_error(&rejected.source_root.join("static/app.js"), "stopped");
    assert!(!rejected.entry_path().exists());
}

#[test]
fn splitting_preserves_the_order_of_two_opaque_loader_requests() {
    let fixture = ProgramPlacement::new(
        "import { basename } from 'node:path'; import { fileURLToPath } from 'node:url'; const name = basename('/tmp/example'); console.log(fileURLToPath('file:///tmp/example'), name);",
    ).place_in_entry(logical_module("name", &[Member::new("name")])).run();
    assert_preserved_entry_output(&fixture, "/tmp/example example\n");
}

#[test]
fn moving_an_unindexed_package_request_into_a_different_loader_scope_refuses() {
    let files = [
        (
            "node_modules/toolkit/package.json",
            r#"{"type":"module","exports":"./index.js"}"#,
        ),
        (
            "node_modules/toolkit/index.js",
            "export const identify = () => 'root';",
        ),
        (
            "static/app/modules/node_modules/toolkit/package.json",
            r#"{"type":"module","exports":"./index.js"}"#,
        ),
        (
            "static/app/modules/node_modules/toolkit/index.js",
            "export const identify = () => 'nested';",
        ),
    ];
    let rejected = run_rejection_fixture(
        FixtureOpts::new(
            "import { identify } from 'toolkit'; const name = identify(); console.log(name);",
            vec![logical_module("name", &[Member::new("name")])],
        )
        .with_extra_files(&files),
    );
    assert!(
        rejected.stderr.contains("resolution_directory"),
        "{}",
        rejected.stderr
    );
    assert_node_output(&rejected.source_root.join("static/app.js"), "root\n", "");
    assert!(!rejected.entry_path().exists());
}

#[test]
fn a_private_uncaptured_native_initialization_can_cross_an_unknown_host_call() {
    let fixture = ProgramPlacement::new(
        "console.log('before'); var seed = new Set(['a', 'b']); console.log('after', seed.size); export {};",
    ).place_in_entry(logical_module("seed", &[Member::new("seed")])).run();
    assert_preserved_entry_output(&fixture, "before\nafter 2\n");
}

#[test]
fn private_nested_literals_keep_their_normal_noninvoking_completion_receipt() {
    let fixture = ProgramPlacement::new(
        "console.log('before'); const seed = [{ value: 1 }]; console.log('after', seed.length);",
    )
    .place_in_entry(logical_module("seed", &[Member::new("seed")]))
    .run();
    assert_preserved_entry_output(&fixture, "before\nafter 1\n");
}

#[test]
fn an_opaque_host_reentry_cannot_observe_a_moved_captured_native_cell() {
    let rejected = run_rejection_fixture(FixtureOpts::new(
        "import { runInThisContext } from 'node:vm'; globalThis.inspect = () => seed; runInThisContext(\"console.log(globalThis.inspect())\"); var seed = new Set(['a', 'b']); console.log(seed.size);",
        vec![logical_module("seed", &[Member::new("seed")])],
    ));
    assert_node_output(
        &rejected.source_root.join("static/app.js"),
        "undefined\n2\n",
        "",
    );
    assert!(!rejected.entry_path().exists());
}

#[test]
fn an_escaped_direct_eval_cannot_observe_a_moved_uncaptured_native_cell() {
    let rejected = run_rejection_fixture(FixtureOpts::new(
        "import { runInThisContext } from 'node:vm'; globalThis.inspect = () => eval('seed'); runInThisContext(\"console.log(globalThis.inspect())\"); var seed = new Set(['a', 'b']); console.log(seed.size);",
        vec![logical_module("seed", &[Member::new("seed")])],
    ));
    assert_node_output(
        &rejected.source_root.join("static/app.js"),
        "undefined\n2\n",
        "",
    );
    assert!(!rejected.entry_path().exists());
}

#[test]
fn canonical_entry_relocation_does_not_assume_the_same_package_search_directory() {
    let files = [
        (
            "node_modules/toolkit/package.json",
            r#"{"type":"module","exports":"./index.js"}"#,
        ),
        (
            "node_modules/toolkit/index.js",
            "export const identify = () => 'root';",
        ),
        (
            "static/app/node_modules/toolkit/package.json",
            r#"{"type":"module","exports":"./index.js"}"#,
        ),
        (
            "static/app/node_modules/toolkit/index.js",
            "export const identify = () => 'chunk directory';",
        ),
    ];
    let rejected = run_rejection_fixture(
        FixtureOpts::new(
            "import { identify } from 'toolkit'; const name = identify(); console.log(name);",
            vec![],
        )
        .with_chunk_renames(chunk_rename("readable_name", "name"))
        .with_unassigned_mode(unassigned_mode_inline())
        .with_extra_files(&files),
    );
    assert!(
        rejected.stderr.contains("resolution_directory"),
        "{}",
        rejected.stderr
    );
    assert_node_output(&rejected.source_root.join("static/app.js"), "root\n", "");
    assert!(!rejected.entry_path().exists());
}

#[test]
fn a_host_setter_observes_the_original_captured_cell_before_its_later_update() {
    let opts = || {
        FixtureOpts::new(
            "let later = 0; const callback = () => later; globalThis.saved = callback; const computed = 264; later = 42; console.log(computed, callback());",
            vec![
                logical_module_with_anon(
                    "compute",
                    &[Member::new("callback"), Member::new("computed")],
                    &["globalThis.saved = callback;"],
                ),
                logical_module_with_anon("later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
    };
    let refused = run_rejection_fixture(opts());
    let report: serde_json::Value =
        read_json(&refused.report_root.join("evaluation_schedule.json"));
    assert_eq!(report["kind"], "evaluation_schedule_refused");
    assert!(
        report["rejection"]["reason"]
            .as_str()
            .unwrap()
            .contains("evaluation_schedule_unknown_reordered"),
        "{report}"
    );
    assert!(!refused.entry_path().exists());

    // This tree is explicitly rejected. Executing it demonstrates divergence,
    // never acceptance or an equivalent source reconstruction.
    let diagnostic = run_emit_on_rejection_fixture(opts());
    assert!(diagnostic.gate_rejected_marker().is_file());
    let setup = "Object.defineProperty(globalThis, 'saved', { set(callback) { console.log('setter', callback()); }, configurable: true });\n";
    let source_driver = diagnostic.source_root.join("host-setter.mjs");
    write_text_file(
        &source_driver,
        &format!("{setup}await import('./static/app.js');\n"),
    );
    assert_node_output(&source_driver, "setter 0\n264 42\n", "");
    let emitted_driver = diagnostic.app_root().join("host-setter.mjs");
    write_text_file(
        &emitted_driver,
        &format!("{setup}await import('./static/app/entry.js');\n"),
    );
    assert_node_error(
        &emitted_driver,
        "Cannot access 'later' before initialization",
    );
    assert!(
        std::fs::read_to_string(diagnostic.entry_path())
            .unwrap()
            .starts_with("// GATE_REJECTED:")
    );
}

#[test]
fn bare_node_builtins_keep_closed_loader_identity_after_entry_relocation() {
    let fixture = ProgramPlacement::new(
        "import assert from 'assert'; import { basename } from 'path'; import { AsyncLocalStorage } from 'async_hooks'; console.log(typeof assert, basename('/tmp/example'), typeof AsyncLocalStorage);",
    ).run();
    assert_preserved_entry_output(&fixture, "function example function\n");
}

#[test]
fn an_unindexed_absolute_file_backedge_cannot_reload_the_original_entry() {
    let fixture = |extra_args: &[&str]| {
        let workspace = write_tree_fixture(&TreeFixture {
            chunks: &[("main", "console.log('entry'); export {};\n")],
            module_roots: &[],
            modules: &[],
        });
        let external = workspace.root().join("external.mjs");
        write_text_file(
            &external,
            "import './snapshot/main.js'; console.log('external');\n",
        );
        write_text_file(
            &workspace.root().join("package.json"),
            r#"{"type":"module"}"#,
        );
        write_text_file(
            &workspace.root().join("snapshot/main.js"),
            &format!(
                "import {}; console.log('entry'); export {{}};\n",
                serde_json::to_string(&format!("file://{}", external.display())).unwrap(),
            ),
        );
        workspace.run(extra_args)
    };
    let rejected = fixture(&[]);
    assert_eq!(
        rejected.result.status.code(),
        Some(1),
        "{}",
        rejected.result.stderr
    );
    assert!(
        rejected
            .result
            .stderr
            .contains("evaluation_schedule_unsupported"),
        "{}",
        rejected.result.stderr
    );
    assert_node_output(
        &rejected.root().join("snapshot/main.js"),
        "external\nentry\n",
        "",
    );
    assert!(!rejected.out_root.join("app/main/entry.js").exists());

    let diagnostic = fixture(&["--emit-on-rejection"]);
    assert_eq!(
        diagnostic.result.status.code(),
        Some(3),
        "{}",
        diagnostic.result.stderr
    );
    let entry = diagnostic.out_root.join("app/main/entry.js");
    assert!(
        std::fs::read_to_string(&entry)
            .unwrap()
            .contains("GATE_REJECTED")
    );
    // The opaque file imports snapshot/main.js. In the emitted graph that is
    // a second application entry, so its evaluation runs in addition to ours.
    assert_node_output(&entry, "entry\nexternal\nentry\n", "");
}
