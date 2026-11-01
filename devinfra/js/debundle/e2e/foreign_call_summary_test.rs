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
fn central_direct_return_migration_preserves_literal_input_retention() {
    // Migration controls: these supported cases also pass the former strict
    // source evaluator. They assert the external runtime and summary contract.
    for (returned, projection) in [
        ("{ value }", "wrapped.value()"),
        ("[value]", "wrapped[0]()"),
        ("{ nested: [{ value }] }", "wrapped.nested[0].value()"),
    ] {
        let source = format!(
            "import {{ make }} from './peer.js';\nconst wrapped = make(() => later, () => later);\nconst later = 42;\nconsole.log({projection});\n"
        );
        let provider = format!("export function make(value, unused) {{ return {returned}; }}\n");
        let fixture = run_fixture(
            FixtureOpts::new(&source, modules()).with_extra_chunks(&[("static/peer", &provider)]),
        );
        assert_preserved_entry_output(&fixture, "42\n");
        let report: serde_json::Value =
            read_json(&fixture.report_root.join("static/app/modules.json"));
        assert_eq!(
            report["imported_callables"]["make"]["foreign_call"]["retained_arguments"],
            serde_json::json!([0])
        );
    }
}

#[test]
fn erased_or_uncovered_provider_operations_do_not_get_a_strict_summary() {
    // Conservative coverage controls, not baseline-failing regressions. In
    // particular, a global store can be absent from SynchronousCalls despite
    // a complete outcome; the full canonical body must still reject it.
    for (provider, observed) in [
        (
            "export function make(input) { globalThis.flag = 1; return { input }; }",
            1,
        ),
        (
            "export function make(input) { return (globalThis.flag = 1, { input }); }",
            1,
        ),
        (
            "export function make(input) { return { value: (globalThis.flag = 1) }; }",
            1,
        ),
        ("export function make(input) { return `${input}`; }", 0),
        ("export function make(input) { return +input; }", 0),
        (
            "export function make(input) { return input ? { input } : {}; }",
            0,
        ),
        (
            "export function make(input) { return { value: input.value }; }",
            0,
        ),
        ("export function make(input = {}) { return { input }; }", 0),
        ("export function make({ value }) { return { value }; }", 0),
        ("export async function make(input) { return { input }; }", 0),
        ("export function* make(input) { return { input }; }", 0),
        ("export function make(input) { throw input; }", 0),
        (
            "export function make(input) { try { return { input }; } catch { return {}; } }",
            0,
        ),
        ("export function make(input) { return { [input]: 1 }; }", 0),
        ("export function make(input) { return { ...input }; }", 0),
        (
            "export function make(input) { return { __proto__: input }; }",
            0,
        ),
    ] {
        let fixture = run_fixture(
            FixtureOpts::new("import { make } from './peer.js';\nglobalThis.flag = 0;\ntry { make(1); } catch {}\nconsole.log(globalThis.flag);\n", vec![])
                .with_extra_chunks(&[("static/peer", provider)]),
        );
        assert_preserved_entry_output(&fixture, &format!("{observed}\n"));
        let report: serde_json::Value =
            read_json(&fixture.report_root.join("static/app/modules.json"));
        assert!(
            report["imported_callables"]["make"]["foreign_call"].is_null(),
            "uncovered provider: {provider}"
        );
    }
}

// Preservation controls for live retained inputs. Existing broad fallback can
// already preserve their ordering; these are not baseline-failing regressions.
fn assert_live_retained_input(setup: &str, mutation: &str, use_result: &str) {
    let provider = format!(
        "export function wrap(value) {{ return {{ value }}; }}\nexport function use(box) {{ return {use_result}; }}\n"
    );
    let source = format!(
        r#"import {{ wrap, use }} from './peer.js';
function read() {{ try {{ return later; }} catch (error) {{ return error.name; }} }}
{setup}
const wrapped = wrap(input);
{mutation}
const observed = use(wrapped);
const later = 42;
console.log(observed);
"#
    );
    let fixture = run_fixture(
        FixtureOpts::new(&source, vec![])
            .with_extra_chunks(&[("static/peer", provider.as_str())])
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "ReferenceError\n");
    let graph = fixture.dependency_graph();
    let observed = source_unit_for_binding(&graph, "observed");
    let later = source_unit_for_binding(&graph, "later");
    assert!(graph.edges.iter().any(|edge| {
        edge.source == observed
            && edge.target == later
            && edge.binding.as_deref() == Some("later")
            && edge.constrains_init_order
    }));
}

#[test]
fn returned_result_preserves_live_input_aliases() {
    assert_live_retained_input(
        "const input = {}; const alias = input;",
        "alias.read = read;",
        "box.value.read()",
    );
}

#[test]
fn returned_result_preserves_cyclic_input_reach() {
    assert_live_retained_input(
        "const input = {}; input.self = input;",
        "input.self.read = read;",
        "box.value.self.read()",
    );
}

#[test]
fn returned_result_keeps_array_insertion_conservative() {
    assert_live_retained_input("const input = [];", "input.push(read);", "box.value[0]()");
}

#[test]
fn returned_result_keeps_map_key_insertion_conservative() {
    assert_live_retained_input(
        "const input = new Map();",
        "input.set(read, 1);",
        "[...box.value.keys()][0]()",
    );
}

#[test]
fn returned_result_keeps_map_value_insertion_conservative() {
    assert_live_retained_input(
        "const input = new Map();",
        "input.set('read', read);",
        "box.value.get('read')()",
    );
}

#[test]
fn returned_result_keeps_set_insertion_conservative() {
    assert_live_retained_input(
        "const input = new Set();",
        "input.add(read);",
        "[...box.value][0]()",
    );
}

#[test]
fn equal_retained_inputs_do_not_prove_result_identity_or_shape() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { wrap } from './peer.js';\nconst input = {};\nconst first = wrap(input);\nconst second = wrap(input);\nconst selected = first.value;\nconsole.log(first === second, selected === second.value);\n",
            vec![],
        )
        .with_extra_chunks(&[("static/peer", "export function wrap(value) { return { value }; }\n")]),
    );
    assert_preserved_entry_output(&fixture, "false true\n");
    let graph = fixture.dependency_graph();
    let selected = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "selected")
        })
        .expect("selected owner");
    assert!(
        !selected
            .synchronous_execution
            .as_ref()
            .expect("execution")
            .complete,
        "an opaque returned carrier must not prove an ordinary own-data read"
    );
}

#[test]
fn opaque_result_does_not_certify_an_audited_factory_input() {
    let source = r#"import { wrap, outer } from './peer.js';
const input = { get read() { try { return later; } catch (error) { return error.name; } } };
const intermediate = wrap(input);
const wrapped = outer(intermediate);
const later = 42;
console.log(wrapped.value);
"#;
    let provider = "export function wrap(value) { return { value }; }\nexport function outer(value) { return { value: value.value.read }; }\n";
    let assertion = ChunkExportPurityBuilder::new("static/peer")
        .with_retained_callback_factory("outer", 1, &[0])
        .build();
    let colocated = run_fixture(
        FixtureOpts::new(source, vec![])
            .with_extra_chunks(&[("static/peer", provider)])
            .with_chunk_export_purity(std::slice::from_ref(&assertion))
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&colocated, "ReferenceError\n");
    // The outer contract excludes accessors in its input graph. An opaque
    // foreign result does not establish that eligibility condition.
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[
                        Member::new("input"),
                        Member::new("intermediate"),
                        Member::new("wrapped"),
                    ],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", provider)])
        .with_chunk_export_purity(&[assertion]),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
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

#[test]
fn an_initializer_export_gets_no_call_or_record_summary() {
    // An importer can call it before the provider's statement has run, which
    // neither consumer models as a possible throw. Its callback-factory
    // projection is covered by `imported_callback_timing`.
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { make } from './peer.js';\nconst queue = make();\nconsole.log(queue.size);\n",
            vec![],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const make = function () { const state = new Map(); return { get size() { return state.size; } }; };\n",
        )]),
    );
    assert_preserved_entry_output(&fixture, "0\n");
    let report: serde_json::Value = read_json(&fixture.report_root.join("static/app/modules.json"));
    let summary = &report["imported_callables"]["make"];
    assert!(summary["foreign_call"].is_null(), "{summary}");
    assert!(summary["returned_record"].is_null(), "{summary}");
}
