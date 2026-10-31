//! Acyclic ESM importers cannot re-enter a provider during its evaluation.

use debundle_e2e_support::*;

const PROVIDER: &str = r#"const prefix = new globalThis.Date();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return "ready";
})({});
const later = dynamic;
class Registry { static current = later; }
export function readLater() { return Registry.current; }
"#;

fn expect_provider_conflict(
    provider: &'static str,
    extra_chunks: &[(&'static str, &'static str)],
    extra_files: &[(&'static str, &'static str)],
) {
    expect_provider_conflict_with_overrides(provider, extra_chunks, extra_files, &[]);
}

fn expect_provider_conflict_with_overrides(
    provider: &'static str,
    extra_chunks: &[(&'static str, &'static str)],
    extra_files: &[(&'static str, &'static str)],
    admission_overrides: &'static [&'static str],
) {
    let provider_modules = vec![
        logical_module(
            "values/dynamic",
            &[Member::new("prefix"), Member::new("dynamic")],
        ),
        logical_module(
            "values/later",
            &[
                Member::new("later"),
                Member::new("Registry"),
                Member::new("readLater"),
            ],
        ),
    ];
    let opts = FixtureOpts::new(provider, provider_modules)
        .with_chunk_id("static/provider")
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
            logical_module(
                "values/dynamic",
                &[Member::new("prefix"), Member::new("dynamic")],
            ),
            logical_module(
                "values/later",
                &[
                    Member::new("later"),
                    Member::new("Registry"),
                    Member::new("readLater"),
                ],
            ),
        ],
    )];
    let provider_chunks = [("static/provider", PROVIDER)];
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"import { readLater } from "./provider.js";
export { readLater };
"#,
            vec![],
        )
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
    let provider = r#"import { enter } from "./consumer.js";
const prefix = new globalThis.Date();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return "ready";
})({});
enter();
const later = dynamic;
class Registry { static current = later; }
export function readLater() { return Registry.current; }
"#;
    let consumer = r#"import { readLater } from "./provider.js";
export { readLater };
export function enter() { return readLater(); }
"#;
    expect_provider_conflict(provider, &[("static/consumer", consumer)], &[]);
}

#[test]
fn named_reexport_backedge_keeps_export_fallback() {
    let provider = r#"import { enter } from "./consumer.js";
const prefix = new globalThis.Date();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return "ready";
})({});
enter();
const later = dynamic;
class Registry { static current = later; }
export function readLater() { return Registry.current; }
"#;
    let consumer = r#"import { readLater } from "./bridge.js";
export { readLater };
export function enter() { return readLater(); }
"#;
    let bridge = r#"export { readLater } from "./provider.js";
"#;
    expect_provider_conflict(
        provider,
        &[("static/consumer", consumer), ("static/bridge", bridge)],
        &[],
    );
}

#[test]
fn star_reexport_backedge_keeps_export_fallback() {
    let provider = r#"import { enter } from "./consumer.js";
const prefix = new globalThis.Date();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return "ready";
})({});
enter();
const later = dynamic;
class Registry { static current = later; }
export function readLater() { return Registry.current; }
"#;
    let consumer = r#"import { readLater } from "./bridge.js";
export { readLater };
export function enter() { return readLater(); }
"#;
    let bridge = r#"export * from "./provider.js";
"#;
    expect_provider_conflict(
        provider,
        &[("static/consumer", consumer), ("static/bridge", bridge)],
        &[],
    );
}

#[test]
fn unresolved_import_in_provider_closure_keeps_export_fallback() {
    let provider = r#"import "./outside.js";
const prefix = new globalThis.Date();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return "ready";
})({});
const later = dynamic;
class Registry { static current = later; }
export function readLater() { return Registry.current; }
"#;
    let consumer = r#"import { readLater } from "./provider.js";
export { readLater };
"#;
    expect_provider_conflict(
        provider,
        &[("static/consumer", consumer)],
        &[("static/outside.js", "export {};\n")],
    );
}

#[test]
fn unresolved_source_reexport_keeps_export_fallback() {
    let provider = r#"export * from "./outside.js";
const prefix = new globalThis.Date();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return "ready";
})({});
const later = dynamic;
class Registry { static current = later; }
export function readLater() { return Registry.current; }
"#;
    let consumer = r#"import { readLater } from "./provider.js";
export { readLater };
"#;
    expect_provider_conflict(
        provider,
        &[("static/consumer", consumer)],
        &[("static/outside.js", "export const outsideValue = 0;\n")],
    );
}

#[test]
fn top_level_await_in_an_unselected_dependency_keeps_export_fallback() {
    let provider = r#"import "./bridge.js";
const prefix = new globalThis.Date();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return "ready";
})({});
const later = dynamic;
class Registry { static current = later; }
export function readLater() { return Registry.current; }
"#;
    let consumer = r#"import { readLater } from "./provider.js";
export { readLater };
"#;
    expect_provider_conflict(
        provider,
        &[
            ("static/consumer", consumer),
            ("static/bridge", "await Promise.resolve();\nexport {};\n"),
        ],
        &[],
    );
}

#[test]
fn passing_the_reader_to_an_opaque_constructor_keeps_export_fallback() {
    let provider = r#"const prefix = new globalThis.Error(readLater);
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return "ready";
})({});
const later = dynamic;
class Registry { static current = later; }
export function readLater() { return Registry.current; }
"#;
    let consumer = r#"import { readLater } from "./provider.js";
export { readLater };
"#;
    expect_provider_conflict(provider, &[("static/consumer", consumer)], &[]);
}

#[test]
fn global_reader_leak_after_an_opaque_call_keeps_export_fallback() {
    let provider = r#"const prefix = new globalThis.Date();
globalThis.reader = readLater;
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return "ready";
})({});
const later = dynamic;
class Registry { static current = later; }
export function readLater() { return Registry.current; }
"#;
    let consumer = r#"import { readLater } from "./provider.js";
export { readLater };
"#;
    expect_provider_conflict(provider, &[("static/consumer", consumer)], &[]);
}

#[test]
fn global_require_member_alias_keeps_export_fallback() {
    let provider = r#"const load = globalThis["require"];
const prefix = new globalThis.Date();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return "ready";
})({});
const later = dynamic;
class Registry { static current = later; }
export function readLater() { return Registry.current; }
"#;
    let consumer = r#"import { readLater } from "./provider.js";
export { readLater };
"#;
    expect_provider_conflict(provider, &[("static/consumer", consumer)], &[]);
}

#[test]
fn import_meta_require_member_alias_keeps_export_fallback() {
    let provider = r#"const load = import.meta.require;
globalThis.loader = load;
const prefix = new globalThis.Date();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return "ready";
})({});
const later = dynamic;
class Registry { static current = later; }
export function readLater() { return Registry.current; }
"#;
    let consumer = r#"import { readLater } from "./provider.js";
export { readLater };
"#;
    expect_provider_conflict_with_overrides(
        provider,
        &[("static/consumer", consumer)],
        &[],
        &["a5_import_meta"],
    );
}
