//! Acyclic ESM importers cannot re-enter a provider during its evaluation.

use debundle_e2e_support::*;

const DATE: &str = "new globalThis.Date()";
const CONSUMER: &str = r#"import { readLater } from "./provider.js";
export { readLater };
"#;

/// The provider module, with the statements that decide whether an importer can re-enter it
/// spliced in around the shared body.
fn provider_source(
    head: &str,
    prefix_init: &str,
    after_prefix: &str,
    before_later: &str,
) -> String {
    format!(
        r#"{head}const prefix = {prefix_init};
{after_prefix}const dynamic = ((object) => {{
  object[(object.key = 0)] = "value";
  return "ready";
}})({{}});
{before_later}const later = dynamic;
class Registry {{ static current = later; }}
export function readLater() {{ return Registry.current; }}
"#
    )
}

/// The provider that calls back into its importer before `later` is initialized.
fn entering_provider() -> String {
    provider_source(
        "import { enter } from \"./consumer.js\";\n",
        DATE,
        "",
        "enter();\n",
    )
}

fn expect_provider_conflict(
    provider: &str,
    extra_chunks: &[(&'static str, &'static str)],
    extra_files: &[(&'static str, &'static str)],
) {
    expect_provider_conflict_with_overrides(
        provider,
        extra_chunks,
        extra_files,
        &[],
        &["values/dynamic", "values/later"],
    );
}

fn expect_provider_conflict_with_overrides(
    provider: &str,
    extra_chunks: &[(&'static str, &'static str)],
    extra_files: &[(&'static str, &'static str)],
    admission_overrides: &'static [&'static str],
    priority: &[&str],
) {
    let provider_modules = vec![
        logical_module("values/dynamic", &binding_members(["prefix", "dynamic"])),
        logical_module(
            "values/later",
            &binding_members(["later", "Registry", "readLater"]),
        ),
    ];
    let opts = FixtureOpts::new(provider, provider_modules)
        .with_chunk_id("static/provider")
        .with_import_order(priority)
        .with_extra_chunks(extra_chunks)
        .with_extra_files(extra_files)
        .with_admission_overrides(admission_overrides);
    expect_atomic_conflict_rejection(opts, &["values/dynamic", "values/later"], &[]);
}

#[test]
fn acyclic_consumer_cannot_reenter_exported_reader_during_provider_evaluation() {
    let provider_modules = [(
        "static/provider",
        vec![
            logical_module("values/dynamic", &binding_members(["prefix", "dynamic"])),
            logical_module(
                "values/later",
                &binding_members(["later", "Registry", "readLater"]),
            ),
        ],
    )];
    let provider = provider_source("", DATE, "", "");
    let provider_chunks = [("static/provider", provider.as_str())];
    let fixture = run_fixture(
        FixtureOpts::new(CONSUMER, vec![])
            .with_chunk_id("static/consumer")
            .with_extra_chunks(&provider_chunks)
            .with_extra_chunk_logical_modules(&provider_modules),
    );

    assert_entry_output(&fixture, "");
    assert_generated_module_after_entry_script(
        &fixture,
        "const consumer = await import('./static/consumer/entry.js');\nconsole.log(consumer.readLater());\n",
        "ready\n",
    );
    assert_module_exports(
        &fixture.out_root,
        "static/consumer/entry.js",
        &["readLater"],
        &[],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/provider/modules/values/later.js",
        &["readLater"],
        &[],
    );
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn cyclic_consumer_can_reenter_exported_reader_during_provider_evaluation() {
    let consumer = r#"import { readLater } from "./provider.js";
export { readLater };
export function enter() { return readLater(); }
"#;
    expect_provider_conflict(&entering_provider(), &[("static/consumer", consumer)], &[]);
}

#[test]
fn named_reexport_backedge_keeps_export_fallback() {
    let consumer = r#"import { readLater } from "./bridge.js";
export { readLater };
export function enter() { return readLater(); }
"#;
    let bridge = r#"export { readLater } from "./provider.js";
"#;
    expect_provider_conflict(
        &entering_provider(),
        &[("static/consumer", consumer), ("static/bridge", bridge)],
        &[],
    );
}

#[test]
fn star_reexport_backedge_keeps_export_fallback() {
    let consumer = r#"import { readLater } from "./bridge.js";
export { readLater };
export function enter() { return readLater(); }
"#;
    let bridge = r#"export * from "./provider.js";
"#;
    expect_provider_conflict(
        &entering_provider(),
        &[("static/consumer", consumer), ("static/bridge", bridge)],
        &[],
    );
}

#[test]
fn unresolved_import_in_provider_closure_keeps_export_fallback() {
    let provider = provider_source("import \"./outside.js\";\n", DATE, "", "");
    expect_provider_conflict(
        &provider,
        &[("static/consumer", CONSUMER)],
        &[("static/outside.js", "export {};\n")],
    );
}

#[test]
fn unresolved_source_reexport_keeps_export_fallback() {
    let provider = provider_source("export * from \"./outside.js\";\n", DATE, "", "");
    expect_provider_conflict(
        &provider,
        &[("static/consumer", CONSUMER)],
        &[("static/outside.js", "export const outsideValue = 0;\n")],
    );
}

#[test]
fn top_level_await_in_an_unselected_dependency_keeps_export_fallback() {
    let provider = provider_source("import \"./bridge.js\";\n", DATE, "", "");
    expect_provider_conflict(
        &provider,
        &[
            ("static/consumer", CONSUMER),
            ("static/bridge", "await Promise.resolve();\nexport {};\n"),
        ],
        &[],
    );
}

#[test]
fn passing_the_reader_to_an_opaque_constructor_keeps_export_fallback() {
    let provider = provider_source("", "new globalThis.Error(readLater)", "", "");
    expect_provider_conflict(&provider, &[("static/consumer", CONSUMER)], &[]);
}

#[test]
fn global_reader_leak_after_an_opaque_call_keeps_export_fallback() {
    let provider = provider_source("", DATE, "globalThis.reader = readLater;\n", "");
    expect_provider_conflict(&provider, &[("static/consumer", CONSUMER)], &[]);
}

#[test]
fn global_require_member_alias_keeps_export_fallback() {
    let provider = provider_source("const load = globalThis[\"require\"];\n", DATE, "", "");
    expect_provider_conflict_with_overrides(
        &provider,
        &[("static/consumer", CONSUMER)],
        &[],
        &[],
        &["values/dynamic", "values/later", "residual"],
    );
}

#[test]
fn import_meta_require_member_alias_keeps_export_fallback() {
    let provider = provider_source(
        "const load = import.meta.require;\nglobalThis.loader = load;\n",
        DATE,
        "",
        "",
    );
    expect_provider_conflict_with_overrides(
        &provider,
        &[("static/consumer", CONSUMER)],
        &[],
        &["a5_import_meta"],
        &["values/dynamic", "values/later", "residual"],
    );
}
