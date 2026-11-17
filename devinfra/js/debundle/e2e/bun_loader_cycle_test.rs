//! Pinned Bun coverage for an actually called synchronous loader cycle.

use debundle_e2e_support::*;

const APP: &str = r#"function readLater() {
  return later;
}

function loadPeer() {
  return import.meta.require("./peer.js");
}

const observed = loadPeer().observed;
const later = 7;

console.log(`${observed}|${later}`);
export { readLater, loadPeer, observed, later };
"#;

const PEER: &str = r#"const app = import.meta.require("./app.js");
let observed;
try {
  app.readLater();
  observed = "value";
} catch (error) {
  observed = error instanceof ReferenceError ? "tdz" : error.name;
}
export { observed };
"#;

fn partial_namespace_host() -> HostBehavior {
    HostBehavior {
        synchronous_module_loader: SynchronousModuleLoader::Present,
        loader_result_for_evaluating_module: Some(
            LoaderResultForEvaluatingModule::ReturnsPartialNamespace,
        ),
        synchronous_load_runs_app_callbacks: Some(SynchronousLoadRunsAppCallbacks::Never),
        ..HostBehavior::default()
    }
}

#[test]
fn colocated_late_binding_preserves_called_cycle_observation_under_pinned_bun() {
    let fixture = run_fixture(
        FixtureOpts::new(
            APP,
            vec![logical_module(
                "core",
                &binding_members(["readLater", "loadPeer", "observed", "later"]),
            )],
        )
        .with_admission_overrides(&["a5_import_meta"])
        .with_extra_chunks(&[("static/peer", PEER)])
        .with_host_behavior(partial_namespace_host()),
    );

    assert_module_source(
        &fixture.out_root,
        "static/app/modules/core.js",
        &["import.meta.require", "../../peer/entry.js"],
        &["./peer.js"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/peer/entry.js",
        &["import.meta.require", "../app/entry.js"],
        &["./app.js"],
    );
    assert_preserved_entry_output_with_bun(&fixture, "tdz|7\n");
}

#[test]
fn splitting_the_late_binding_from_the_cycle_is_rejected() {
    let rejected = run_rejection_fixture(
        FixtureOpts::new(
            APP,
            vec![
                logical_module(
                    "cycle_path",
                    &binding_members(["readLater", "loadPeer", "observed"]),
                ),
                logical_module("late_state", &[Member::new("later")]),
            ],
        )
        .with_admission_overrides(&["a5_import_meta"])
        .with_extra_chunks(&[("static/peer", PEER)])
        .with_host_behavior(partial_namespace_host())
        .with_graphs(),
    );
    let conflicts = rejected.atomic_group_conflicts();
    assert!(
        conflicts.iter().any(|conflict| {
            let claims = conflict["claims"].as_array().expect("conflict claims");
            ["cycle_path", "late_state"].iter().all(|module| {
                claims
                    .iter()
                    .any(|claim| claim["module"].as_str() == Some(module))
            })
        }),
        "splitting the late binding should produce an atomic-group conflict: {conflicts:#?}",
    );
}
