//! Constructor bodies and imported binding initialization are independent proofs.

use debundle_e2e_graph_support::FixtureGraphExt;
use debundle_e2e_support::*;

const OBSERVE: &str = "console.log(marker, wrapped === copied, wrapped instanceof Box);";

const SOURCE: &str = "import { Box } from './peer.js';\nconst marker = 1;\nconst wrapped = new Box();\nconst copied = wrapped;\nconsole.log(marker, wrapped === copied, wrapped instanceof Box);\n";

fn modules(observation: &str) -> Vec<LogicalModuleEntry> {
    vec![
        logical_module("instance", &[Member::new("wrapped"), Member::new("copied")]),
        logical_module_with_anon("observe", &[Member::new("marker")], &[observation]),
    ]
}

fn execution(fixture: &impl FixtureGraphExt) -> serde_json::Value {
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|node| {
            node["declared_bindings"]
                .as_array()
                .is_some_and(|bindings| {
                    bindings
                        .iter()
                        .any(|binding| binding["binding"] == "wrapped")
                })
        })
        .unwrap()["synchronous_execution"]
        .clone()
}

#[test]
fn isolated_imported_constructor_uses_shared_body_effects_for_real_relocation() {
    for provider in [
        "export class Box {}",
        "class Box { constructor() {} } export { Box };",
        "export class Box { constructor() { return 1; } }",
        "export class Box { method() { return globalThis.value; } }",
        "export class Box { value; }",
        "export class Box { #value; }",
        "export class Box { value = 1; value = 2; }",
        "export class Box { value; #value; constructor(callback) { this.#value = callback; } }",
    ] {
        let fixture = run_fixture(
            FixtureOpts::new(SOURCE, modules(OBSERVE))
                .with_extra_chunks(&[("static/peer", provider)]),
        );
        assert_preserved_entry_output(&fixture, "1 true true\n");
        let execution = execution(&fixture);
        assert_eq!(execution["complete"], true, "{provider}: {execution:#}");
        assert_eq!(
            execution["binding_effect_authority"]["authority"], "operation_receipt",
            "{provider}: {execution:#}"
        );
        let report = fixture.effect_translation_validation();
        assert!(report["swapped_pairs"].as_u64().unwrap() > 0, "{report:#}");
        assert_eq!(report["decision"]["status"], "accepted", "{report:#}");
    }
}

#[test]
fn public_constructor_assignment_requires_an_existing_writable_own_field() {
    let source = "import { Box } from './peer.js';\nconst marker = 1;\nconst wrapped = new Box(42);\nconst copied = wrapped;\nconsole.log(marker, wrapped.field, wrapped.count, wrapped === copied, wrapped instanceof Box);\n";
    for provider in [
        "export class Box { field; count; constructor(value) { let count = 0; this.field = (count = count + 1, value); this.count = count; } }",
        "export class Box { field; count; set field(value) { throw new Error('setter'); } constructor(value) { let count = 0; this.field = (count = count + 1, value); this.count = count; } }",
    ] {
        let fixture = run_fixture(
            FixtureOpts::new(source, modules("console.log(marker, wrapped.field, wrapped.count, wrapped === copied, wrapped instanceof Box);"))
                .with_extra_chunks(&[("static/peer", provider)]),
        );
        assert_preserved_entry_output(&fixture, "1 42 1 true true\n");
        let execution = execution(&fixture);
        assert_eq!(execution["complete"], true, "{provider}: {execution:#}");
        assert_eq!(
            execution["binding_effect_authority"]["authority"], "operation_receipt",
            "{provider}: {execution:#}"
        );
        let report = fixture.effect_translation_validation();
        assert!(report["swapped_pairs"].as_u64().unwrap() > 0, "{report:#}");
        assert_eq!(report["decision"]["status"], "accepted", "{report:#}");
    }
}

#[test]
fn prototype_setter_constructor_assignment_keeps_callback_execution_uncovered() {
    let source = "import { Box } from './peer.js';\nconst marker = 1;\nconst wrapped = new Box(() => console.log('setter'));\nconst copied = wrapped;\nconsole.log(marker, wrapped === copied, wrapped instanceof Box);\n";
    let provider = "export class Box { set field(value) { value(); } constructor(value) { this.field = value; } }";
    let colocated = run_fixture(
        FixtureOpts::new(source, vec![]).with_extra_chunks(&[("static/peer", provider)]),
    );
    assert_preserved_entry_output(&colocated, "setter\n1 true true\n");
    let rejected = run_rejection_fixture(
        FixtureOpts::new(source, modules(OBSERVE)).with_extra_chunks(&[("static/peer", provider)]),
    );
    let execution = execution(&rejected);
    assert_ne!(
        execution["binding_effect_authority"]["authority"], "operation_receipt",
        "{execution:#}"
    );
}

#[test]
fn unsupported_constructor_operations_keep_conservative_effects() {
    for provider in [
        "export class Box { #method() {} }",
        "export class Box { get #value() { return 1; } }",
        "export class Box { constructor() { var value = 1; return value; } }",
        "export class Box { static { globalThis.fixtureConstructorMarker = 1; } }",
        "export class Box { static value = 1; }",
        "export class Box { ['method']() {} }",
        "export class Box { constructor(value) { this.field = value; } }",
        "export class Box { #field; constructor(value) { this.field = value; } }",
    ] {
        let colocated = run_fixture(
            FixtureOpts::new(SOURCE, vec![]).with_extra_chunks(&[("static/peer", provider)]),
        );
        assert_preserved_entry_output(&colocated, "1 true true\n");
        let rejected = run_rejection_fixture(
            FixtureOpts::new(SOURCE, modules(OBSERVE))
                .with_extra_chunks(&[("static/peer", provider)]),
        );
        let execution = execution(&rejected);
        assert_ne!(
            execution["binding_effect_authority"]["authority"], "operation_receipt",
            "{provider}: {execution:#}"
        );
    }
}

#[test]
fn constructor_body_closure_does_not_initialize_a_cyclic_import() {
    let provider = "import './app.js'; export class Box {}";
    let colocated = run_fixture(
        FixtureOpts::new(SOURCE, vec![]).with_extra_chunks(&[("static/peer", provider)]),
    );
    assert_preserved_entry_output(&colocated, "1 true true\n");
    let rejected = run_rejection_fixture(
        FixtureOpts::new(SOURCE, modules(OBSERVE)).with_extra_chunks(&[("static/peer", provider)]),
    );
    let execution = execution(&rejected);
    assert_ne!(
        execution["binding_effect_authority"]["authority"], "operation_receipt",
        "{execution:#}"
    );
}
