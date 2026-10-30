//! A declared host loader keeps the static-importer export-reentry proof only
//! when every loader site in the program is loader-closed for the target.

use debundle_e2e_support::*;
use serde_json::{Value, json};

const PROVIDER_CHUNK: &str = "static/provider";

/// An opaque call precedes later exported state: under the conservative
/// policy the call can reenter `readLater`, binding the two modules together.
const PROVIDER: &str = r#"const prefix = new globalThis.Error();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return "ready";
})({});
const later = dynamic;
class Registry { static current = later; }
export function readLater() { return Registry.current; }
export function loadHelper() { return import.meta.require("./helper.js"); }
"#;
const HELPER: &str = "exports.suffix = \"!\";\n";
const CONSUMER: &str = r#"import { readLater } from "./provider.js";
export { readLater };
"#;

fn audited_host() -> HostBehavior {
    HostBehavior {
        loader_result_for_evaluating_module: Some(LoaderResultForEvaluatingModule::Throws),
        synchronous_load_runs_app_callbacks: Some(SynchronousLoadRunsAppCallbacks::Never),
        reentry_free_builtin_modules: [NodeBuiltinModule::Path].into_iter().collect(),
        ..HostBehavior::default()
    }
}

fn provider_modules() -> Vec<LogicalModuleEntry> {
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
                Member::new("loadHelper"),
            ],
        ),
    ]
}

fn export_reentry_report(report_root: &std::path::Path) -> Value {
    read_json(&report_root.join(PROVIDER_CHUNK).join("export_reentry.json"))
}

/// Run the split provider under `host`, assert the gate rejects it, and return
/// the provider's loader derivation.
fn rejected_loader_derivation(
    provider: &'static str,
    extra_chunks: &[(&'static str, &'static str)],
    host: HostBehavior,
) -> Value {
    let rejected = run_rejection_fixture(
        FixtureOpts::new(provider, provider_modules())
            .with_chunk_id(PROVIDER_CHUNK)
            .with_extra_chunks(extra_chunks)
            .with_host_behavior(host),
    );
    let conflicts = rejected.atomic_group_conflicts();
    assert!(
        conflicts.iter().any(|conflict| {
            let modules: Vec<_> = conflict["claims"]
                .as_array()
                .unwrap()
                .iter()
                .map(|claim| claim["module"].as_str().unwrap())
                .collect();
            modules.contains(&"values/dynamic") && modules.contains(&"values/later")
        }),
        "{conflicts:#?}"
    );
    let report = export_reentry_report(&rejected.report_root);
    assert_eq!(report["policy"], "conservative");
    assert_eq!(report["loader"]["rule"], "loader_closure");
    assert_eq!(report["loader"]["closed"], false);
    report["loader"].clone()
}

fn failure<'a>(derivation: &'a Value, reason: &str) -> &'a Value {
    derivation["failures"]
        .as_array()
        .unwrap()
        .iter()
        .find(|failure| failure["reason"] == reason)
        .unwrap_or_else(|| panic!("no {reason} failure: {derivation:#}"))
}

#[test]
fn closed_commonjs_request_keeps_the_static_importer_proof() {
    let extra_chunks = [("static/helper", HELPER), ("static/provider", PROVIDER)];
    let provider_modules = [(PROVIDER_CHUNK, provider_modules())];
    let opts = || {
        FixtureOpts::new(CONSUMER, vec![])
            .with_chunk_id("static/consumer")
            .with_extra_chunks(&extra_chunks)
            .with_extra_chunk_logical_modules(&provider_modules)
    };
    // Without the loader keys the same split is bound by the opaque call.
    let conservative = run_rejection_fixture(opts());
    let conflicts: Vec<Value> = read_json(
        &conservative
            .report_root
            .join(PROVIDER_CHUNK)
            .join("atomic_group_conflicts.json"),
    );
    assert!(!conflicts.is_empty());
    assert_eq!(
        export_reentry_report(&conservative.report_root)["loader"]["failures"][0]["reason"],
        "undeclared_host_behavior"
    );

    let fixture = run_fixture(opts().with_host_behavior(audited_host()));
    let report = export_reentry_report(&fixture.report_root);
    assert_eq!(report["policy"], "no_static_importer_reentry");
    assert_eq!(
        report["loader"],
        json!({
            "rule": "loader_closure",
            "closed": true,
            "host_keys": [
                "loader_result_for_evaluating_module",
                "synchronous_load_runs_app_callbacks",
            ],
            "requests": [{
                "source_chunk": PROVIDER_CHUNK,
                "specifier": "./helper.js",
                "target": { "chunk": { "chunk": "static/helper", "file": "entry.js" } },
                "request_count": 1,
            }],
            "loaded_chunks": ["static/helper"],
        })
    );
    // `loadHelper` is never called: Node has no `import.meta.require`.
    assert_generated_module_after_entry_script(
        &fixture,
        "const consumer = await import('./static/consumer/entry.js');\nconsole.log(consumer.readLater());\n",
        "ready\n",
    );
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn non_literal_request_anywhere_keeps_the_target_open() {
    let sidecar = "export function loadNamed(name) { return import.meta.require(name); }\n";
    let derivation = rejected_loader_derivation(
        PROVIDER,
        &[("static/helper", HELPER), ("static/sidecar", sidecar)],
        audited_host(),
    );
    let first = &failure(&derivation, "opaque_loader_site")["first"]["site"];
    assert_eq!(first["source_chunk"], "static/sidecar");
    assert_eq!(first["kind"], "import_meta_require_dynamic_call");
}

#[test]
fn aliased_loader_keeps_the_target_open() {
    let sidecar = "export function loader() { return import.meta.require; }\n";
    let derivation = rejected_loader_derivation(
        PROVIDER,
        &[("static/helper", HELPER), ("static/sidecar", sidecar)],
        audited_host(),
    );
    let first = &failure(&derivation, "opaque_loader_site")["first"]["site"];
    assert_eq!(first["source_chunk"], "static/sidecar");
    assert_eq!(first["kind"], "import_meta_require_reference");
}

#[test]
fn loaded_module_requesting_the_target_keeps_it_open() {
    let helper = "exports.back = () => import.meta.require(\"./provider.js\");\n";
    let derivation =
        rejected_loader_derivation(PROVIDER, &[("static/helper", helper)], audited_host());
    let first = &failure(&derivation, "loader_request_reaches_target")["first"]["site"];
    assert_eq!(first["source_chunk"], "static/helper");
    assert_eq!(first["resolved_target"]["chunk"], PROVIDER_CHUNK);
}

/// Soundness anchor: the loaded module reads the requester while it evaluates.
#[test]
fn loaded_module_calling_back_into_the_requester_at_load_keeps_it_open() {
    let provider = PROVIDER.replace(
        "export function loadHelper() { return import.meta.require(\"./helper.js\"); }\n",
        "const helper = (() => import.meta.require(\"./helper.js\"))();\nexport function loadHelper() { return helper; }\n",
    );
    let helper = "const provider = import.meta.require(\"./provider.js\");\nexports.seen = provider.readLater();\n";
    let derivation = rejected_loader_derivation(
        Box::leak(provider.into_boxed_str()),
        &[("static/helper", helper)],
        audited_host(),
    );
    assert_eq!(
        failure(&derivation, "loader_request_reaches_target")["first"]["site"]["source_chunk"],
        "static/helper"
    );
}

#[test]
fn builtin_outside_the_reentry_free_list_keeps_the_target_open() {
    let provider = PROVIDER.replace("./helper.js", "node:events");
    let derivation =
        rejected_loader_derivation(Box::leak(provider.into_boxed_str()), &[], audited_host());
    let first = &failure(&derivation, "non_reentry_free_builtin_request")["first"]["site"];
    assert_eq!(first["specifier"], "node:events");
}

#[test]
fn unknown_or_omitted_loader_keys_keep_the_target_open() {
    for (host, key) in [
        (
            HostBehavior {
                synchronous_load_runs_app_callbacks: Some(SynchronousLoadRunsAppCallbacks::Unknown),
                ..audited_host()
            },
            "synchronous_load_runs_app_callbacks",
        ),
        (
            HostBehavior {
                loader_result_for_evaluating_module: None,
                ..audited_host()
            },
            "loader_result_for_evaluating_module",
        ),
    ] {
        let derivation = rejected_loader_derivation(PROVIDER, &[("static/helper", HELPER)], host);
        let undeclared = failure(&derivation, "undeclared_host_behavior");
        assert_eq!(undeclared["first"], json!({ "host_behavior": key }));
        assert_eq!(undeclared["occurrences"], 1);
    }
}

/// Provider without a host loader site; `import()` alone must not open it.
const DYNAMIC_PROVIDER: &str = r#"const pending = import("./lazy.js");
const prefix = new globalThis.Error();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return "ready";
})({});
const later = dynamic;
class Registry { static current = later; }
export function readLater() { return Registry.current; }
"#;

fn dynamic_provider_modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module(
            "values/dynamic",
            &[
                Member::new("pending"),
                Member::new("prefix"),
                Member::new("dynamic"),
            ],
        ),
        logical_module(
            "values/later",
            &[
                Member::new("later"),
                Member::new("Registry"),
                Member::new("readLater"),
            ],
        ),
    ]
}

/// The dynamically imported module reads the requester at its top level. That
/// happens in a later job, after the requester finished evaluating, so the
/// split preserves the original output. `lazy.js` is a runtime-only sibling:
/// the source copy reads the original provider, the emitted copy (where the
/// rebased specifier lands) reads the emitted provider entry.
#[test]
fn dynamic_import_is_not_a_synchronous_loader_site() {
    let consumer = r#"import { readLater } from "./provider.js";
console.log("consumer", readLater());
"#;
    let extra_chunks = [("static/provider", DYNAMIC_PROVIDER)];
    let extra_files = [
        (
            "static/lazy.js",
            "import { readLater } from \"./provider.js\";\nconsole.log(\"lazy\", readLater());\n",
        ),
        (
            "static/provider/lazy.js",
            "import { readLater } from \"./entry.js\";\nconsole.log(\"lazy\", readLater());\n",
        ),
    ];
    let provider_modules = [(PROVIDER_CHUNK, dynamic_provider_modules())];
    let fixture = run_fixture(
        FixtureOpts::new(consumer, vec![])
            .with_chunk_id("static/consumer")
            .with_extra_chunks(&extra_chunks)
            .with_extra_files(&extra_files)
            .with_extra_chunk_logical_modules(&provider_modules),
    );
    let report = export_reentry_report(&fixture.report_root);
    assert_eq!(report["policy"], "no_static_importer_reentry");
    assert_eq!(report["loader"], json!({ "rule": "no_host_loader_sites" }));
    assert_preserved_entry_output(&fixture, "consumer ready\nlazy ready\n");
    assert_all_emitted_js_checks(&fixture);
}

fn microtask_free_host() -> HostBehavior {
    HostBehavior {
        synchronous_load_drains_microtasks: Some(SynchronousLoadDrainsMicrotasks::Never),
        ..audited_host()
    }
}

/// A non-literal request after an `await` on every path runs in a promise job.
#[test]
fn mandatorily_suspended_site_does_not_hold_the_target_open() {
    let sidecar =
        "export async function loadNamed(name) { await null; return import.meta.require(name); }\n";
    let extra_chunks = [
        ("static/helper", HELPER),
        ("static/provider", PROVIDER),
        ("static/sidecar", sidecar),
    ];
    let provider_modules = [(PROVIDER_CHUNK, provider_modules())];
    let fixture = run_fixture(
        FixtureOpts::new(CONSUMER, vec![])
            .with_chunk_id("static/consumer")
            .with_extra_chunks(&extra_chunks)
            .with_extra_chunk_logical_modules(&provider_modules)
            .with_host_behavior(microtask_free_host()),
    );
    let report = export_reentry_report(&fixture.report_root);
    assert_eq!(report["policy"], "no_static_importer_reentry");
    assert_eq!(report["loader"]["suspended_sites"], 1);
    assert_eq!(
        report["loader"]["host_keys"],
        json!([
            "loader_result_for_evaluating_module",
            "synchronous_load_runs_app_callbacks",
            "synchronous_load_drains_microtasks",
        ])
    );
    assert_generated_module_after_entry_script(
        &fixture,
        "const consumer = await import('./static/consumer/entry.js');\nconsole.log(consumer.readLater());\n",
        "ready\n",
    );
}

#[test]
fn suspension_needs_the_microtask_key_and_an_await_on_every_path() {
    let suspended =
        "export async function loadNamed(name) { await null; return import.meta.require(name); }\n";
    let conditional = "export async function loadNamed(name, wait) { if (wait) await null; return import.meta.require(name); }\n";
    let operand =
        "export async function loadNamed(name) { return await import.meta.require(name); }\n";
    let caught = "export async function loadNamed(name) { try { await null; } catch { return import.meta.require(name); } }\n";
    for (sidecar, host) in [
        (suspended, audited_host()),
        (conditional, microtask_free_host()),
        (operand, microtask_free_host()),
        (caught, microtask_free_host()),
    ] {
        let derivation = rejected_loader_derivation(
            PROVIDER,
            &[("static/helper", HELPER), ("static/sidecar", sidecar)],
            host,
        );
        let first = &failure(&derivation, "opaque_loader_site")["first"]["site"];
        assert_eq!(first["source_chunk"], "static/sidecar", "{sidecar}");
        assert!(derivation.get("suspended_sites").is_none(), "{sidecar}");
    }
}
