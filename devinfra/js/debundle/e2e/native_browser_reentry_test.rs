//! Native browser ESM cannot synchronously expose an evaluating namespace.

use debundle_e2e_support::*;

const PROVIDER: &str = r#"const prefix = new globalThis.Error();
export const value = "ready";
export function read() { return value; }
"#;

fn provider_opts<'a>(source: &'a str) -> FixtureOpts<'a> {
    let mut opts = FixtureOpts::new(
        source,
        vec![
            logical_module("startup", &[Member::new("prefix")]),
            logical_module("values", &[Member::new("value"), Member::new("read")]),
        ],
    );
    opts.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    opts
}

#[test]
fn unrelated_commonjs_cannot_expose_native_browser_exports() {
    let chunks = [("static/other", "export const load = typeof require;\n")];
    let fixture = run_fixture(provider_opts(PROVIDER).with_extra_chunks(&chunks));
    assert_generated_module_after_entry_script(
        &fixture,
        "const app = await import('./static/app/entry.js'); console.log(app.read());\n",
        "ready\n",
    );
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn generic_runtime_keeps_loader_fallback() {
    let chunks = [("static/other", "export const load = typeof require;\n")];
    let mut opts = provider_opts(PROVIDER).with_extra_chunks(&chunks);
    opts.execution_runtime = ExecutionRuntime::Generic;
    expect_atomic_conflict_rejection(opts, &["startup", "values"], &[]);
}

#[test]
fn published_callback_still_constrains_browser_startup() {
    let source = r#"export function read() { return value; }
globalThis.callback = read;
const prefix = new globalThis.Error();
export const value = "ready";
"#;
    expect_atomic_conflict_rejection(provider_opts(source), &["startup", "values"], &[]);
}

#[test]
fn inert_global_slot_publication_is_currently_treated_as_reentry() {
    let source = r#"globalThis.inspectState = () => later;
const startup = new globalThis.Error();
const later = 42;
console.log(later);
"#;
    let mut colocated = FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "startup/all",
            &[Member::new("startup"), Member::new("later")],
            &["globalThis.inspectState = () => later;"],
        )],
    );
    colocated.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    assert_preserved_entry_output(&run_fixture(colocated), "42\n");
    let mut opts = FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "debug/exposure",
                &[],
                &["globalThis.inspectState = () => later;"],
            ),
            logical_module("startup", &[Member::new("startup")]),
            logical_module("state/later", &[Member::new("later")]),
        ],
    );
    opts.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    opts.deferred_global_publications = &[("globalThis", "inspectState")];
    let fixture = run_fixture(opts);
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn no_global_slot_publication_allows_same_startup_split() {
    let source = r#"const unusedReader = () => later;
const startup = new globalThis.Error();
const later = 42;
console.log(later);
"#;
    let mut opts = FixtureOpts::new(
        source,
        vec![
            logical_module("debug/exposure", &[Member::new("unusedReader")]),
            logical_module("startup", &[Member::new("startup")]),
            logical_module("state/later", &[Member::new("later")]),
        ],
    );
    opts.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    let fixture = run_fixture(opts);
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn typeof_window_guarded_publication_uses_same_certificate() {
    let source = r#"import './prelude.js';
typeof window < 'u' && (window.inspectState = () => later);
const startup = new globalThis.Error();
const later = 42;
console.log(later);
"#;
    let files = [
        (
            "static/app/prelude.js",
            "globalThis.window = globalThis; export {};\n",
        ),
        (
            "static/prelude.js",
            "globalThis.window = globalThis; export {};\n",
        ),
    ];
    let modules = || {
        vec![
            logical_module_with_anon(
                "debug/exposure",
                &[],
                &["typeof window < 'u' && (window.inspectState = () => later);"],
            ),
            logical_module("startup", &[Member::new("startup")]),
            logical_module("state/later", &[Member::new("later")]),
        ]
    };
    let mut unasserted = FixtureOpts::new(source, modules()).with_extra_files(&files);
    unasserted.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    expect_atomic_conflict_rejection(unasserted, &["startup", "state/later"], &[]);

    let mut asserted = FixtureOpts::new(source, modules()).with_extra_files(&files);
    asserted.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    asserted.deferred_global_publications = &[("window", "inspectState")];
    let fixture = run_fixture(asserted);
    assert_preserved_entry_output(&fixture, "42\n");
    assert_generated_module_after_entry_script(
        &fixture,
        "console.log(globalThis.inspectState());\n",
        "42\n",
    );
}

#[test]
fn deferred_global_certificate_does_not_cover_another_slot_or_cross_root_read() {
    let source = r#"globalThis.otherState = () => later;
const startup = new globalThis.Error();
const later = 42;
console.log(later);
"#;
    let modules = vec![
        logical_module_with_anon(
            "debug/exposure",
            &[],
            &["globalThis.otherState = () => later;"],
        ),
        logical_module("startup", &[Member::new("startup")]),
        logical_module("state/later", &[Member::new("later")]),
    ];
    let mut opts = FixtureOpts::new(source, modules);
    opts.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    opts.deferred_global_publications = &[("globalThis", "inspectState")];
    expect_atomic_conflict_rejection(opts, &["startup", "state/later"], &[]);

    let source = r#"globalThis.inspectState = () => later;
typeof window !== 'undefined' && window.inspectState;
const startup = new globalThis.Error();
const later = 42;
console.log(later);
"#;
    let mut opts = FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "debug/exposure",
                &[],
                &["globalThis.inspectState = () => later;"],
            ),
            logical_module("startup", &[Member::new("startup")]),
            logical_module("state/later", &[Member::new("later")]),
        ],
    );
    opts.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    opts.deferred_global_publications = &[("globalThis", "inspectState")];
    expect_atomic_conflict_rejection(opts, &["startup", "state/later"], &[]);
}

#[test]
fn deferred_global_certificate_rejects_visible_descriptor_or_alias() {
    for before in [
        "Object.defineProperty(globalThis, 'inspectState', {set(fn) { globalThis.seen = fn(); }});",
        "const alias = globalThis; alias.inspectState = () => later;",
        "const alias = ({root: globalThis}).root; Object.defineProperty(alias, 'inspectState', {set(fn) { globalThis.seen = fn(); }});",
    ] {
        let source = format!(
            "let later = 0;\n{before}\nglobalThis.inspectState = () => later;\nconst startup = new globalThis.Error();\nlater = 42;\nconsole.log(later);\n"
        );
        let mut opts = FixtureOpts::new(
            &source,
            vec![
                logical_module_with_anon(
                    "debug/exposure",
                    &[],
                    &["globalThis.inspectState = () => later;"],
                ),
                logical_module("startup", &[Member::new("startup")]),
                logical_module_with_anon("state/later", &[Member::new("later")], &["later = 42;"]),
            ],
        );
        opts.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
        opts.deferred_global_publications = &[("globalThis", "inspectState")];
        expect_atomic_conflict_rejection(opts, &["startup", "state/later"], &[]);
    }
}

#[test]
fn replaced_window_alias_cannot_certify_a_global_write() {
    let setup = "globalThis.window = new Proxy(globalThis, {set(target, key, value) { if (key === 'inspectState') globalThis.observed = value(); return Reflect.set(target, key, value); }});";
    let write = "typeof window < 'u' && (window.inspectState = () => later);";
    let source = format!(
        "let later = 0;\n{setup}\n{write}\nconst startup = new globalThis.Error();\nlater = 42;\nconsole.log(globalThis.observed, later);\n"
    );
    let mut colocated = FixtureOpts::new(
        &source,
        vec![logical_module_with_anon(
            "state/all",
            &[Member::new("startup"), Member::new("later")],
            &[setup, write, "later = 42;"],
        )],
    );
    colocated.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    assert_preserved_entry_output(&run_fixture(colocated), "0 42\n");

    let mut split = FixtureOpts::new(
        &source,
        vec![
            logical_module_with_anon("debug/exposure", &[], &[setup, write]),
            logical_module("startup", &[Member::new("startup")]),
            logical_module_with_anon("state/later", &[Member::new("later")], &["later = 42;"]),
        ],
    );
    split.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    split.deferred_global_publications = &[("window", "inspectState")];
    expect_atomic_conflict_rejection(split, &["startup", "state/later"], &[]);
}

#[test]
fn direct_read_and_call_of_published_slot_stays_ordered() {
    let source = r#"let later = 0;
globalThis.inspectState = () => later;
const startup = globalThis.inspectState();
later = 42;
console.log(startup, later);
"#;
    let mut colocated = FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "state/all",
            &[Member::new("startup"), Member::new("later")],
            &["globalThis.inspectState = () => later;", "later = 42;"],
        )],
    );
    colocated.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    assert_preserved_entry_output(&run_fixture(colocated), "0 42\n");

    let mut split = FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "debug/exposure",
                &[],
                &["globalThis.inspectState = () => later;"],
            ),
            logical_module("startup", &[Member::new("startup")]),
            logical_module_with_anon("state/later", &[Member::new("later")], &["later = 42;"]),
        ],
    );
    split.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    split.deferred_global_publications = &[("globalThis", "inspectState")];
    expect_atomic_conflict_rejection(split, &["startup", "state/later"], &[]);
}

#[test]
fn separately_exported_reader_keeps_generic_reentry() {
    let source = r#"import { tick } from './peer.js';
export function reader() { return later; }
globalThis.inspectState = reader;
const startup = tick();
const later = 42;
console.log(startup, later);
"#;
    let files = [("static/peer.js", "export function tick() { return 0; }\n")];
    let mut opts = FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "debug/exposure",
                &[Member::new("reader")],
                &["globalThis.inspectState = reader;"],
            ),
            logical_module("startup", &[Member::new("startup")]),
            logical_module("state/later", &[Member::new("later")]),
        ],
    )
    .with_extra_files(&files);
    opts.deferred_global_publications = &[("globalThis", "inspectState")];
    expect_atomic_conflict_rejection(opts, &["startup", "state/later"], &[]);
}

#[test]
fn cycle_keeps_export_reentry_in_browser() {
    let source = r#"import './other.js';
const prefix = new globalThis.Error();
export const value = "ready";
export function read() { return value; }
"#;
    let chunks = [("static/other", "import './app.js'; export {};\n")];
    expect_atomic_conflict_rejection(
        provider_opts(source).with_extra_chunks(&chunks),
        &["startup", "values"],
        &[],
    );
}

#[test]
fn missing_and_pruned_inputs_cannot_certify_browser_closure() {
    let source = r#"import './missing.js';
const prefix = new globalThis.Error();
export const value = "ready";
export function read() { return value; }
"#;
    let files = [("static/missing.js", "export {};\n")];
    expect_atomic_conflict_rejection(
        provider_opts(source).with_extra_files(&files),
        &["startup", "values"],
        &[],
    );
    let chunks = [("static/other", "export {};\n")];
    expect_atomic_conflict_rejection(
        provider_opts(PROVIDER)
            .with_extra_chunks(&chunks)
            .with_prune_other_chunks(),
        &["startup", "values"],
        &[],
    );
}
