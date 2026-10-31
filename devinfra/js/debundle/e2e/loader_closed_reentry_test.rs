//! A declared host loader keeps the static-importer export-reentry proof only
//! when every loader site in the program is loader-closed for the target.

use debundle_e2e_support::*;
use serde_json::{Value, json};

const PROVIDER_CHUNK: &str = "static/provider";

/// An opaque call precedes later exported state: under the conservative
/// policy the call can reenter `readLater`, binding the two modules together.
const PROVIDER: &str = r#"const prefix = new globalThis.Date();
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
const prefix = new globalThis.Date();
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

fn accepted_provider_report(extra: &[(&'static str, &'static str)]) -> Value {
    accepted_provider_report_under(extra, audited_host(), ProgramPremises::default())
}

fn accepted_provider_report_under(
    extra: &[(&'static str, &'static str)],
    host: HostBehavior,
    premises: ProgramPremises,
) -> Value {
    let mut extra_chunks = vec![("static/helper", HELPER), ("static/provider", PROVIDER)];
    extra_chunks.extend_from_slice(extra);
    let provider_modules = [(PROVIDER_CHUNK, provider_modules())];
    let fixture = run_fixture(
        FixtureOpts::new(CONSUMER, vec![])
            .with_chunk_id("static/consumer")
            .with_extra_chunks(&extra_chunks)
            .with_extra_chunk_logical_modules(&provider_modules)
            .with_host_behavior(host)
            .with_program_premises(premises),
    );
    let report = export_reentry_report(&fixture.report_root);
    assert_eq!(report["policy"], "no_static_importer_reentry", "{report:#}");
    assert_generated_module_after_entry_script(
        &fixture,
        "const consumer = await import('./static/consumer/entry.js');\nconsole.log(consumer.readLater());\n",
        "ready\n",
    );
    report
}

/// No reachable code enters `loadNamed`: neither a call nor an escape.
#[test]
fn site_in_a_function_no_reachable_code_enters_does_not_hold_the_target_open() {
    let sidecar = r#"function loadNamed(name) { return import.meta.require(name); }
function unused() { return loadNamed("./provider.js"); }
export const ready = 1;
"#;
    let report = accepted_provider_report(&[("static/sidecar", sidecar)]);
    assert_eq!(report["loader"]["unreachable_sites"], 1);
}

#[test]
fn entered_or_escaping_functions_keep_their_sites() {
    for sidecar in [
        "function loadNamed(name) { return import.meta.require(name); }\nexport const ready = loadNamed;\n",
        "function loadNamed(name) { return import.meta.require(name); }\nfunction start() { return loadNamed('x'); }\nstart();\n",
        "const loadNamed = (name) => import.meta.require(name);\nsetTimeout(() => loadNamed('x'));\n",
    ] {
        let derivation = rejected_loader_derivation(
            PROVIDER,
            &[("static/helper", HELPER), ("static/sidecar", sidecar)],
            audited_host(),
        );
        let first = &failure(&derivation, "opaque_loader_site")["first"]["site"];
        assert_eq!(first["source_chunk"], "static/sidecar", "{sidecar}");
    }
}

const RUNTIME: &str = "const load = import.meta.require;\nexport { load };\n";

/// Every use of the exported loader is a literal call: each is a request
/// resolved against the module whose `import.meta` the loader came from.
#[test]
fn exported_loader_alias_with_literal_calls_is_classified() {
    let user = r#"import { load } from "./runtime.js";
export function readPath() { return load("node:path"); }
"#;
    let report = accepted_provider_report(&[("static/runtime", RUNTIME), ("static/user", user)]);
    let requests = report["loader"]["requests"].as_array().unwrap();
    assert!(
        requests
            .iter()
            .any(|request| request["source_chunk"] == "static/user"
                && request["target"] == json!({ "builtin": "path" })),
        "{requests:#?}"
    );
}

#[test]
fn unclassifiable_uses_of_an_exported_loader_keep_the_target_open() {
    for (user, reason, form) in [
        (
            "import { load } from \"./runtime.js\";\nexport function readNamed(name) { return load(name); }\n",
            "opaque_loader_site",
            "loader_binding_use",
        ),
        (
            "import { load } from \"./runtime.js\";\nexport const loader = load;\n",
            "opaque_loader_site",
            "loader_binding_use",
        ),
        (
            "import * as runtime from \"./runtime.js\";\nexport { runtime };\n",
            "opaque_loader_site",
            "namespace_request",
        ),
        (
            "export { load } from \"./runtime.js\";\n",
            "opaque_loader_site",
            "namespace_request",
        ),
        // Resolved against the runtime module: `./provider.js` is the target.
        (
            "import { load } from \"./runtime.js\";\nexport function back() { return load(\"./provider.js\"); }\n",
            "loader_request_reaches_target",
            "loader_binding_use",
        ),
    ] {
        let derivation = rejected_loader_derivation(
            PROVIDER,
            &[
                ("static/helper", HELPER),
                ("static/runtime", RUNTIME),
                ("static/user", user),
            ],
            audited_host(),
        );
        let first = &failure(&derivation, reason)["first"]["site"];
        assert_eq!(first["source_chunk"], "static/user", "{user}");
        assert_eq!(first["form"], form, "{user}");
    }
}

/// Bun's own modules and the Node builtins added to the closed list are
/// admitted only through their declared lists.
#[test]
fn declared_bun_modules_and_node_builtins_are_loader_leaves() {
    let sidecar = r#"export function ffi() { return import.meta.require("bun:ffi"); }
export function proxy() { return import.meta.require("undici"); }
export function legacy() { return import.meta.require("constants"); }
"#;
    let report = accepted_provider_report_under(
        &[("static/sidecar", sidecar)],
        HostBehavior {
            reentry_free_builtin_modules: [NodeBuiltinModule::Constants].into_iter().collect(),
            reentry_free_bun_modules: [BunModule::BunFfi, BunModule::Undici].into_iter().collect(),
            ..audited_host()
        },
        ProgramPremises::default(),
    );
    let targets: Vec<_> = report["loader"]["requests"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|request| request["source_chunk"] == "static/sidecar")
        .map(|request| request["target"]["builtin"].as_str().unwrap())
        .collect();
    assert_eq!(targets, ["bun:ffi", "constants", "undici"]);
    let host_keys = report["loader"]["host_keys"].as_array().unwrap();
    assert!(host_keys.contains(&json!({ "reentry_free_bun_module": "bun:ffi" })));
    assert!(host_keys.contains(&json!({ "reentry_free_builtin_module": "constants" })));

    let derivation = rejected_loader_derivation(
        PROVIDER,
        &[("static/helper", HELPER), ("static/sidecar", sidecar)],
        audited_host(),
    );
    assert_eq!(
        failure(&derivation, "non_reentry_free_builtin_request")["occurrences"],
        3
    );
}

fn native_addon_premise() -> ProgramPremises {
    ProgramPremises {
        non_literal_loader_calls: NonLiteralLoaderCalls::NativeAddonsOnly,
        ..ProgramPremises::default()
    }
}

/// Under the native-addon premise a non-literal loader call, direct or through
/// an exported alias, loads no module that can reenter the target.
#[test]
fn non_literal_loader_calls_are_excluded_under_the_native_addon_premise() {
    for extra in [
        vec![(
            "static/sidecar",
            "export function loadNamed(name) { return import.meta.require(name); }\n",
        )],
        vec![
            ("static/runtime", RUNTIME),
            (
                "static/user",
                "import { load } from \"./runtime.js\";\nexport function readNamed(name) { return load(name); }\n",
            ),
        ],
    ] {
        let report = accepted_provider_report_under(&extra, audited_host(), native_addon_premise());
        assert_eq!(report["loader"]["premised_sites"], 1, "{extra:?}");
        assert_eq!(
            report["loader"]["premise_keys"],
            json!(["non_literal_loader_calls"]),
            "{extra:?}"
        );
        assert_eq!(
            report["program_premises"],
            json!({ "non_literal_loader_calls": "native_addons_only" })
        );
    }
}

/// The premise covers calls only: a loader value that escapes stays opaque.
#[test]
fn escaping_loader_values_stay_opaque_under_the_native_addon_premise() {
    let sidecar = "export function loader() { return import.meta.require; }\n";
    let rejected = run_rejection_fixture(
        FixtureOpts::new(PROVIDER, provider_modules())
            .with_chunk_id(PROVIDER_CHUNK)
            .with_extra_chunks(&[("static/helper", HELPER), ("static/sidecar", sidecar)])
            .with_host_behavior(audited_host())
            .with_program_premises(native_addon_premise()),
    );
    let derivation = export_reentry_report(&rejected.report_root)["loader"].clone();
    assert_eq!(
        failure(&derivation, "opaque_loader_site")["first"]["site"]["kind"],
        "import_meta_require_reference"
    );
}

#[test]
fn process_request_is_a_leaf_under_the_accessor_premise() {
    let sidecar = "export function proc() { return import.meta.require(\"node:process\"); }\n";
    let report = accepted_provider_report_under(
        &[("static/sidecar", sidecar)],
        HostBehavior {
            reentry_free_builtin_modules: [NodeBuiltinModule::Path, NodeBuiltinModule::Process]
                .into_iter()
                .collect(),
            ..audited_host()
        },
        ProgramPremises {
            process_prototype_chain_accessors: ProcessPrototypeChainAccessors::None,
            ..ProgramPremises::default()
        },
    );
    assert_eq!(
        report["loader"]["premise_keys"],
        json!(["process_prototype_chain_accessors"])
    );
    assert!(
        report["loader"]["host_keys"]
            .as_array()
            .unwrap()
            .contains(&json!({ "reentry_free_builtin_module": "process" }))
    );
}

fn module_host() -> HostBehavior {
    HostBehavior {
        reentry_free_builtin_modules: [
            NodeBuiltinModule::Path,
            NodeBuiltinModule::Module,
            NodeBuiltinModule::WorkerThreads,
        ]
        .into_iter()
        .collect(),
        ..audited_host()
    }
}

fn process_premise() -> ProgramPremises {
    ProgramPremises {
        process_prototype_chain_accessors: ProcessPrototypeChainAccessors::None,
        ..ProgramPremises::default()
    }
}

/// `module` uses that make no loader, or a loader used only with host module
/// names or for `resolve`, leave the target closed.
#[test]
fn module_uses_that_make_no_free_loader_are_classified() {
    for (extra, builtin) in [
        (
            vec![
                ("static/runtime", RUNTIME),
                (
                    "static/user",
                    "import { load } from \"./runtime.js\";\nexport function names() { return load(\"module\").builtinModules; }\n",
                ),
            ],
            Some("module"),
        ),
        (
            vec![(
                "static/sidecar",
                "import { createRequire } from \"module\";\nexport function where(base) { const local = createRequire(base); return local.resolve(\"./x.js\"); }\n",
            )],
            None,
        ),
        (
            vec![(
                "static/sidecar",
                "import { createRequire, builtinModules } from \"node:module\";\nconst local = createRequire(\"/\");\nexport const count = builtinModules.length;\nexport function worker() { return local(\"worker_threads\").Worker; }\n",
            )],
            Some("worker_threads"),
        ),
    ] {
        let report = accepted_provider_report_under(&extra, module_host(), process_premise());
        let builtins: Vec<_> = report["loader"]["requests"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|request| request["target"]["builtin"].as_str())
            .collect();
        assert_eq!(
            builtins,
            builtin.into_iter().collect::<Vec<_>>(),
            "{extra:?}"
        );
    }
}

#[test]
fn module_uses_that_expose_a_loader_keep_the_target_open() {
    for (sidecar, reason) in [
        (
            "import { createRequire } from \"module\";\nexport const make = createRequire;\n",
            "loader_factory_request",
        ),
        (
            "import * as m from \"module\";\nexport { m };\n",
            "loader_factory_request",
        ),
        (
            "import Module from \"module\";\nexport function hook() { return Module._load; }\n",
            "loader_factory_request",
        ),
        (
            "import { createRequire } from \"module\";\nconst local = createRequire(\"/\");\nexport function f(name) { return local(name); }\n",
            "opaque_loader_site",
        ),
        (
            "import { createRequire } from \"module\";\nconst local = createRequire(\"/\");\nexport function f() { return local(\"./provider.js\"); }\n",
            "opaque_loader_site",
        ),
        (
            "import { createRequire } from \"module\";\nexport const local = createRequire(\"/\");\n",
            "opaque_loader_site",
        ),
        (
            "import { createRequire } from \"module\";\nconst local = createRequire(\"/\");\nexport const keep = [local];\n",
            "opaque_loader_site",
        ),
    ] {
        let rejected = run_rejection_fixture(
            FixtureOpts::new(PROVIDER, provider_modules())
                .with_chunk_id(PROVIDER_CHUNK)
                .with_extra_chunks(&[("static/helper", HELPER), ("static/sidecar", sidecar)])
                .with_host_behavior(module_host())
                .with_program_premises(process_premise()),
        );
        let derivation = export_reentry_report(&rejected.report_root)["loader"].clone();
        assert_eq!(
            failure(&derivation, reason)["first"]["site"]["source_chunk"],
            "static/sidecar",
            "{sidecar}"
        );
    }
    let user = "import { load } from \"./runtime.js\";\nexport function make() { return load(\"module\").createRequire(\"/\"); }\n";
    let derivation = rejected_loader_derivation(
        PROVIDER,
        &[
            ("static/helper", HELPER),
            ("static/runtime", RUNTIME),
            ("static/user", user),
        ],
        HostBehavior {
            reentry_free_builtin_modules: [NodeBuiltinModule::Path, NodeBuiltinModule::Module]
                .into_iter()
                .collect(),
            ..audited_host()
        },
    );
    assert_eq!(
        failure(&derivation, "loader_factory_request")["first"]["site"]["source_chunk"],
        "static/user"
    );
}

/// An absolute request for an embedded file is a leaf when the manifest's
/// host loader is declared reentry-free; a native addon also needs the
/// native-addon premise.
#[test]
fn embedded_files_are_leaves_under_their_declared_loaders() {
    let manifest_dir = tempfile::tempdir().unwrap();
    let manifest = manifest_dir.path().join("embedded-loaders.json");
    std::fs::write(
        &manifest,
        r#"{"$bunfs/root/guide.md": "text", "$bunfs/root/addon.node": "napi", "$bunfs/root/script.mjs": "file"}"#,
    )
    .unwrap();
    let sidecar = r#"export function guide() { return import.meta.require("/$bunfs/root/guide.md"); }
export function addon() { return import.meta.require("/$bunfs/root/addon.node"); }
"#;
    let host = HostBehavior {
        reentry_free_embedded_loaders: [EmbeddedLoader::Text, EmbeddedLoader::Napi]
            .into_iter()
            .collect(),
        ..audited_host()
    };
    let mut extra_chunks = vec![
        ("static/helper", HELPER),
        ("static/provider", PROVIDER),
        ("static/sidecar", sidecar),
    ];
    let provider_logical = [(PROVIDER_CHUNK, provider_modules())];
    let fixture = run_fixture(
        FixtureOpts::new(CONSUMER, vec![])
            .with_chunk_id("static/consumer")
            .with_extra_chunks(&extra_chunks)
            .with_extra_chunk_logical_modules(&provider_logical)
            .with_host_behavior(host.clone())
            .with_program_premises(native_addon_premise())
            .with_embedded_loaders_path(manifest.clone()),
    );
    let report = export_reentry_report(&fixture.report_root);
    assert_eq!(report["policy"], "no_static_importer_reentry", "{report:#}");
    let embedded: Vec<_> = report["loader"]["requests"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|request| request["target"]["embedded_file"].as_str())
        .collect();
    assert_eq!(embedded, ["napi", "text"]);
    assert_eq!(
        report["loader"]["premise_keys"],
        json!(["non_literal_loader_calls"])
    );

    // A `file` asset (loaded by extension, so `.mjs` runs as code) or an
    // undeclared loader keeps the target open.
    extra_chunks[2] = (
        "static/sidecar",
        "export function script() { return import.meta.require(\"/$bunfs/root/script.mjs\"); }\nexport function guide() { return import.meta.require(\"/$bunfs/root/guide.md\"); }\n",
    );
    let rejected = run_rejection_fixture(
        FixtureOpts::new(PROVIDER, provider_modules())
            .with_chunk_id(PROVIDER_CHUNK)
            .with_extra_chunks(&[("static/helper", HELPER), extra_chunks[2]])
            .with_host_behavior(audited_host())
            .with_embedded_loaders_path(manifest),
    );
    let derivation = export_reentry_report(&rejected.report_root)["loader"].clone();
    assert_eq!(
        failure(&derivation, "non_reentry_free_embedded_file")["occurrences"],
        2
    );
}

#[test]
fn native_addon_loader_requires_the_native_addon_premise() {
    expect_rejection_containing_all(
        FixtureOpts::new("export const value = 1;\n", vec![]).with_host_behavior(HostBehavior {
            reentry_free_embedded_loaders: [EmbeddedLoader::Napi].into_iter().collect(),
            ..HostBehavior::default()
        }),
        &["program_premises.non_literal_loader_calls: native_addons_only"],
    );
}

/// protobufjs `inquire` reaches the module-scope `require` through a direct
/// eval of constant code; that eval is a loader read, not dynamic scope.
#[test]
fn direct_eval_of_require_is_a_loader_call_not_dynamic_scope() {
    let user = r#"import { load } from "./runtime.js";
export function inquire(name) {
  try { return name === "fs" ? load("fs") : eval("quire".replace(/^/, "re"))(name); } catch (e) { return null; }
}
"#;
    let extra = [("static/runtime", RUNTIME), ("static/user", user)];
    let report = accepted_provider_report_under(
        &extra,
        HostBehavior {
            reentry_free_builtin_modules: [NodeBuiltinModule::Path, NodeBuiltinModule::Fs]
                .into_iter()
                .collect(),
            ..audited_host()
        },
        native_addon_premise(),
    );
    assert_eq!(report["loader"]["premised_sites"], 1);

    let derivation = rejected_loader_derivation(
        PROVIDER,
        &[
            ("static/helper", HELPER),
            ("static/runtime", RUNTIME),
            ("static/user", user),
        ],
        audited_host(),
    );
    assert_eq!(
        failure(&derivation, "opaque_loader_site")["first"]["site"]["kind"],
        "evaluated_require_dynamic_call"
    );
}

/// Any other eval code may read the imported loader under any name.
#[test]
fn direct_eval_of_other_code_exposes_imported_loaders() {
    let user = r#"import { load } from "./runtime.js";
export function run(code) { return eval(code + ""); }
"#;
    let derivation = rejected_loader_derivation(
        PROVIDER,
        &[
            ("static/helper", HELPER),
            ("static/runtime", RUNTIME),
            ("static/user", user),
        ],
        audited_host(),
    );
    assert_eq!(
        failure(&derivation, "opaque_loader_site")["first"]["site"]["source_chunk"],
        "static/user"
    );
}

/// A site counts only when it can run while the provider evaluates: a
/// function in a chunk that statically imports the provider runs only after
/// it, and a top-level load in an unrelated chunk runs only with that chunk's
/// body.
#[test]
fn sites_outside_the_target_window_do_not_hold_it_open() {
    for sidecar in [
        "import { readLater } from \"./provider.js\";\nexport function loadNamed(name) { return import.meta.require(name); }\nexport const seen = readLater;\n",
        "const named = globalThis.process ? \"a\" : \"b\";\nexport const loaded = import.meta.require(named);\n",
    ] {
        let report = accepted_provider_report(&[("static/sidecar", sidecar)]);
        assert_eq!(report["loader"]["outside_window_sites"], 1, "{sidecar}");
    }
}
