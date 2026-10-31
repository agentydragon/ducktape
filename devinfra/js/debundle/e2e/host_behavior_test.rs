//! `host_behavior` is validated as one block and echoed as read.

use debundle_e2e_support::*;
use serde_json::{Value, json};

const CHUNK_ID: &str = "static/provider";
const PROVIDER: &str = r#"const prefix = new globalThis.Date();
export const value = "ready";
export function read() { return value; }
"#;
const COMMONJS_SIBLING: &[(&str, &str)] =
    &[("static/other", "export const load = typeof require;\n")];

fn split_provider() -> FixtureOpts<'static> {
    FixtureOpts::new(
        PROVIDER,
        vec![
            logical_module("startup", &[Member::new("prefix")]),
            logical_module("values", &[Member::new("value"), Member::new("read")]),
        ],
    )
    .with_extra_chunks(COMMONJS_SIBLING)
}

fn accepted_provider() -> FixtureOpts<'static> {
    FixtureOpts::new(
        "export const value = 1;\n",
        vec![logical_module("values", &[Member::new("value")])],
    )
    .with_chunk_id(CHUNK_ID)
}

fn export_reentry_report(fixture: &Fixture) -> Value {
    read_json(
        &fixture
            .report_root
            .join(CHUNK_ID)
            .join("export_reentry.json"),
    )
}

#[test]
fn omitted_block_is_the_generic_host() {
    expect_atomic_conflict_rejection(split_provider(), &["startup", "values"], &[]);
    let fixture = run_fixture(accepted_provider());
    assert_eq!(
        export_reentry_report(&fixture)["host_behavior"],
        json!({ "synchronous_module_loader": "present" })
    );
}

#[test]
fn no_synchronous_loader_lifts_the_commonjs_interop_blocker() {
    let fixture = run_fixture(split_provider().with_host_behavior(HostBehavior {
        synchronous_module_loader: SynchronousModuleLoader::None,
        ..HostBehavior::default()
    }));
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn declared_block_is_echoed_as_read_and_unknown_is_accepted() {
    let fixture = run_fixture(accepted_provider().with_host_behavior(HostBehavior {
        synchronous_module_loader: SynchronousModuleLoader::Present,
        loader_result_for_evaluating_module: Some(LoaderResultForEvaluatingModule::Unknown),
        synchronous_load_runs_app_callbacks: Some(SynchronousLoadRunsAppCallbacks::Never),
        synchronous_load_drains_microtasks: Some(SynchronousLoadDrainsMicrotasks::Unknown),
        reentry_free_builtin_modules: [NodeBuiltinModule::Path].into_iter().collect(),
        reentry_free_bun_modules: [BunModule::Ws].into_iter().collect(),
        reentry_free_embedded_loaders: [EmbeddedLoader::Text].into_iter().collect(),
        reentry_free_operations: [ReentryFreeOperation::PromiseResolve].into_iter().collect(),
        predefined_globals: PredefinedGlobals::Standard,
        note: Some("Audited against a synthetic fixture host.".to_string()),
    }));
    assert_eq!(
        export_reentry_report(&fixture)["host_behavior"],
        json!({
            "synchronous_module_loader": "present",
            "loader_result_for_evaluating_module": "unknown",
            "synchronous_load_runs_app_callbacks": "never",
            "synchronous_load_drains_microtasks": "unknown",
            "reentry_free_builtin_modules": ["path"],
            "reentry_free_bun_modules": ["ws"],
            "reentry_free_embedded_loaders": ["text"],
            "reentry_free_operations": ["promise_resolve"],
            "predefined_globals": "standard",
            "note": "Audited against a synthetic fixture host.",
        })
    );
}

#[test]
fn loader_keys_require_a_present_loader() {
    let none = HostBehavior {
        synchronous_module_loader: SynchronousModuleLoader::None,
        ..HostBehavior::default()
    };
    let cases = [
        (
            "loader_result_for_evaluating_module",
            HostBehavior {
                loader_result_for_evaluating_module: Some(LoaderResultForEvaluatingModule::Throws),
                ..none.clone()
            },
        ),
        (
            "synchronous_load_runs_app_callbacks",
            HostBehavior {
                synchronous_load_runs_app_callbacks: Some(SynchronousLoadRunsAppCallbacks::Never),
                ..none.clone()
            },
        ),
        (
            "synchronous_load_drains_microtasks",
            HostBehavior {
                synchronous_load_drains_microtasks: Some(SynchronousLoadDrainsMicrotasks::Unknown),
                ..none.clone()
            },
        ),
    ];
    for (key, host_behavior) in cases {
        expect_rejection_containing_all(
            accepted_provider().with_host_behavior(host_behavior),
            &[
                &format!("host_behavior.{key}"),
                "synchronous_module_loader: present",
            ],
        );
    }
}

#[test]
fn process_accessor_builtins_require_the_program_premise() {
    for builtin in [NodeBuiltinModule::Process, NodeBuiltinModule::WorkerThreads] {
        let host_behavior = HostBehavior {
            reentry_free_builtin_modules: [builtin].into_iter().collect(),
            ..HostBehavior::default()
        };
        expect_rejection_containing_all(
            accepted_provider().with_host_behavior(host_behavior.clone()),
            &["program_premises.process_prototype_chain_accessors: none"],
        );
        let fixture = run_fixture(
            accepted_provider()
                .with_host_behavior(host_behavior)
                .with_program_premises(ProgramPremises {
                    process_prototype_chain_accessors: ProcessPrototypeChainAccessors::None,
                    note: Some("Audited against a synthetic fixture program.".to_string()),
                    ..ProgramPremises::default()
                }),
        );
        assert_eq!(
            export_reentry_report(&fixture)["program_premises"],
            json!({
                "process_prototype_chain_accessors": "none",
                "note": "Audited against a synthetic fixture program.",
            })
        );
    }
}

#[test]
fn retired_keys_are_rejected_beside_host_behavior() {
    for (key, value) in [
        ("execution_runtime", json!("native_browser_esm")),
        (
            "host_no_reentry",
            json!({ "runtime": "bun_standalone_1_3_10", "operations": ["promise_resolve"] }),
        ),
    ] {
        expect_rejection_containing_all(accepted_provider().with_raw_spec_key(key, value), &[key]);
    }
}
