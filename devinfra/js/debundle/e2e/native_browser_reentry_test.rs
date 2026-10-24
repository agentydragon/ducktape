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
