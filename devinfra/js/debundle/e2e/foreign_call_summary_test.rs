//! Derived foreign call summaries (SPEC.md § Derived foreign call summaries):
//! a provider export whose body only allocates is no unknown callee at the
//! importer; anything that can run other code stays conservative.

use analysis::reports::schema::InferenceRule;
use debundle_e2e_support::*;

const BOX: &str = "export class Box {\n  #callback;\n  #cache = new WeakMap();\n  constructor(callback, label) { this.#callback = callback; }\n  invoke() { return this.#callback(); }\n}\n";

fn modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
        logical_module("values/later", &[Member::new("later")]),
    ]
}

fn reject(source: &str, provider: &str) {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(source, modules()).with_extra_chunks(&[("static/peer", provider)]),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn derived_constructor_retains_its_callback_until_instance_use() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { Box } from './peer.js';\nconst wrapped = new Box(() => later, 'label');\nconst later = 42;\nconsole.log(wrapped.invoke());\n",
            modules(),
        )
        .with_extra_chunks(&[("static/peer", BOX)]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
    let report: serde_json::Value = read_json(&fixture.report_root.join("static/app/modules.json"));
    let summary = &report["imported_callables"]["Box"]["foreign_call"];
    assert_eq!(summary["invocation"], "construct");
    assert_eq!(summary["retained_arguments"], serde_json::json!([0]));
    assert_eq!(
        summary["native_constructions"],
        serde_json::json!(["weak_map"])
    );
    let graph = fixture.dependency_graph();
    let wrapped = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "wrapped")
        })
        .expect("wrapped owner");
    let execution = wrapped
        .synchronous_execution
        .as_ref()
        .expect("synchronous execution");
    assert!(execution.complete);
    assert!(
        execution
            .inference_uses
            .iter()
            .any(|usage| usage.conclusion == InferenceRule::ForeignCallSummary)
    );
    let purity = serde_json::to_value(&wrapped.purity).unwrap();
    assert_eq!(purity["kind"], "not_pure");
}

#[test]
fn a_public_field_write_keeps_the_constructor_unknown() {
    reject(
        "import { Box } from './peer.js';\nconst wrapped = new Box(() => later);\nconst later = 42;\nconsole.log(wrapped.invoke());\n",
        "export class Box { constructor(callback) { this.callback = callback; } invoke() { return this.callback(); } }\n",
    );
}

#[test]
fn builder_entry_defers_the_callbacks_in_its_input() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { object } from './peer.js';\nconst wrapped = object({ read: () => later }, 'note');\nconst later = 42;\nconsole.log(wrapped.shape.read());\n",
            modules(),
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export function object(shape, description) { const kind = 'object'; return { kind, shape }; }\n",
        )]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn a_method_on_the_summarized_result_stays_unknown() {
    // The value model has no foreign object identity, so `.describe` is an
    // unknown member call that may run the retained callback.
    reject(
        "import { object } from './peer.js';\nconst wrapped = object({ read: () => later }).describe('note');\nconst later = 42;\nconsole.log(wrapped.shape.read());\n",
        "export function object(shape) { return { shape, describe(text) { return this; } }; }\n",
    );
}

#[test]
fn identity_returns_its_argument_and_drops_the_other() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { keep } from './peer.js';\nconst wrapped = keep({ read: () => later }, () => later);\nconst later = 42;\nconsole.log(wrapped.read());\n",
            modules(),
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export function keep(value, reset) { return value; }\n",
        )]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn a_provider_that_may_invoke_its_callback_stays_unknown() {
    reject(
        "import { untracked } from './peer.js';\nconst wrapped = untracked(() => later, false);\nconst later = 42;\nconsole.log(wrapped, later);\n",
        "export function untracked(callback, run) { if (run) callback(); return 0; }\n",
    );
}

#[test]
fn a_provider_reading_exported_state_stays_unknown() {
    // An importer can replace `registry.lazy` before the call.
    reject(
        "import { lazy } from './peer.js';\nconst wrapped = lazy(() => later);\nconst later = 42;\nconsole.log(wrapped());\n",
        "export const registry = { lazy(e) { return () => e(); } };\nexport function lazy(e) { return registry.lazy(e); }\n",
    );
}

#[test]
fn a_vendor_marked_provider_keeps_its_summary() {
    let vendor_marks = [(
        "static/peer.js",
        VendorMark {
            identity: "replaced-provider".into(),
            role: Default::default(),
            level: VendorLevel::Suppress,
        },
    )];
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { Box } from './peer.js';\nconst wrapped = new Box(() => later);\nconst later = 42;\nconsole.log(wrapped.invoke());\n",
            modules(),
        )
        .with_extra_chunks(&[("static/peer", BOX)])
        .with_vendor_marks(&vendor_marks),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}
