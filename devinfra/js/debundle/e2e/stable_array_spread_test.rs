//! A local primitive array spread can preserve narrow module boundaries.

use debundle_e2e_support::*;
use sha2::{Digest, Sha256};

const SOURCE: &str = r#"const clientBefore = console.log("before");
const first = ["a"], second = ["b"], scopes = ["base", ...first, ...second];
const apiError = scopes;
const clientAfter = apiError;
console.log(scopes.join(","), clientAfter.join(","));
export { scopes, apiError, clientAfter };
"#;

const LITERAL_CONTROL: &str = r#"const clientBefore = console.log("before");
const first = ["a"], second = ["b"], scopes = ["base", "a", "b"];
const apiError = scopes;
const clientAfter = apiError;
console.log(scopes.join(","), clientAfter.join(","));
export { scopes, apiError, clientAfter };
"#;

fn modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module(
            "client",
            &[Member::new("clientBefore"), Member::new("clientAfter")],
        ),
        logical_module("error/schema", &[Member::new("apiError")]),
        logical_module(
            "oauth/scopes",
            &[
                Member::new("first"),
                Member::new("second"),
                Member::new("scopes"),
            ],
        ),
    ]
}

fn spread_input(source: &str, declaration: &str) -> NativeOperationInputContract {
    NativeOperationInputContract {
        source_match: serde_yaml::from_str(&format!(
            "match: {:?}\nbindings: [scopes]\n",
            declaration.trim()
        ))
        .expect("valid exact source selector"),
        operation: NativeOperationKind::ArraySpreadFromPrimitiveArrays,
        audited_source_sha256: Sha256::digest(source.as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect(),
        note: Some("Neutral fixture audits this exact Array iterator protocol.".to_string()),
    }
}

fn declaration(source: &str) -> &str {
    source
        .lines()
        .find(|line| line.starts_with("const first ="))
        .expect("fixture contains the multi-declarator initializer")
}

#[test]
fn same_declaration_primitive_array_spreads_keep_three_modules_realizable() {
    let fixture = run_fixture(FixtureOpts::new(SOURCE, modules()));
    assert_preserved_entry_output(&fixture, "before\nbase,a,b base,a,b\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/oauth/scopes.js",
        &["scopes"],
        &[],
    );
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert!(
        graph["edges"]
            .as_array()
            .unwrap()
            .iter()
            .any(|edge| { edge["edge_kind"] == "co_location" }),
        "certified spread must retain a distinct producer co-location edge"
    );
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn literal_spread_control_keeps_three_modules_realizable() {
    let fixture = run_fixture(FixtureOpts::new(LITERAL_CONTROL, modules()));
    assert_preserved_entry_output(&fixture, "before\nbase,a,b base,a,b\n");
}

#[test]
fn exact_source_input_preserves_runtime_and_reports_consumed_premise() {
    let opts = FixtureOpts::new(SOURCE, modules()).with_native_operation_inputs(
        "static/app",
        vec![spread_input(SOURCE, declaration(SOURCE))],
    );
    let fixture = run_fixture(opts);
    assert_preserved_entry_output(&fixture, "before\nbase,a,b base,a,b\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert!(
        graph["nodes"].as_array().unwrap().iter().any(|node| {
            node["synchronous_execution"]["native_operation_input_uses"]
                .as_array()
                .is_some_and(|uses| !uses.is_empty())
        }),
        "the accepted exact premise must be visible in graph evidence"
    );
}

#[test]
fn direct_array_protocol_write_contradicts_exact_input() {
    for mutation in [
        "Array.prototype[Symbol.iterator] = function* () { yield 'changed'; };",
        "Object.getPrototypeOf([][Symbol.iterator]()).next = function () { return { done: true }; };",
    ] {
        let source = format!(
            "{mutation}\nconst first = ['a'], second = ['b'], scopes = ['base', ...first, ...second];\nexport {{ scopes }};\n"
        );
        let opts = FixtureOpts::new(
            &source,
            vec![logical_module("scopes", &[Member::new("scopes")])],
        )
        .with_native_operation_inputs(
            "static/app",
            vec![spread_input(&source, declaration(&source))],
        );
        let rejected = run_rejection_fixture(opts);
        assert!(
            rejected
                .stderr
                .contains("known Array iterator protocol write"),
            "mutation {mutation:?} was not identified as a known Array iterator write:\n{}",
            rejected.stderr
        );
    }
}

#[test]
fn imported_sibling_protocol_writes_contradict_exact_input() {
    for mutation in [
        "Array.prototype[Symbol.iterator] = function* () { yield 'changed'; };\nexport {};\n",
        "Object.getPrototypeOf([][Symbol.iterator]()).next = function () { return { done: true }; };\nexport {};\n",
    ] {
        let source = SOURCE.replacen(
            "const clientBefore",
            "import './mutation.js';\nconst clientBefore",
            1,
        );
        let extra_chunks = [("static/mutation", mutation)];
        let opts = FixtureOpts::new(&source, modules())
            .with_extra_chunks(&extra_chunks)
            .with_native_operation_inputs(
                "static/app",
                vec![spread_input(&source, declaration(&source))],
            );
        let rejected = run_rejection_fixture(opts);
        assert!(
            rejected.stderr.contains("Array iterator protocol write")
                && rejected.stderr.contains("static/mutation"),
            "mutation {mutation:?} was not identified as an imported Array iterator write:\n{}",
            rejected.stderr
        );
    }
}

#[test]
fn sibling_symbol_iterator_replacement_keeps_other_symbol_key_write_opaque() {
    let source = format!("import './symbol.js';\nimport './mutation.js';\n{SOURCE}");
    let extra_chunks = [
        (
            "static/symbol",
            "globalThis.Symbol = { iterator: Symbol('replacement') };\nexport {};\n",
        ),
        (
            "static/mutation",
            "Array.prototype[Symbol.iterator] = function* () { yield 'changed'; };\nexport {};\n",
        ),
    ];
    let opts = FixtureOpts::new(&source, modules())
        .with_extra_chunks(&extra_chunks)
        .with_native_operation_inputs(
            "static/app",
            vec![spread_input(&source, declaration(&source))],
        );
    let fixture = run_fixture(opts);
    assert_preserved_entry_output(&fixture, "before\nbase,a,b base,a,b\n");
}

#[test]
fn sibling_global_array_replacement_does_not_contradict_literal_array_iterator() {
    let source = format!("import './array.js';\nimport './mutation.js';\n{SOURCE}");
    let extra_chunks = [
        (
            "static/array",
            "globalThis.Array = { prototype: {} };\nexport {};\n",
        ),
        (
            "static/mutation",
            "Array.prototype[Symbol.iterator] = function* () { yield 'changed'; };\nexport {};\n",
        ),
    ];
    let opts = FixtureOpts::new(&source, modules())
        .with_extra_chunks(&extra_chunks)
        .with_native_operation_inputs(
            "static/app",
            vec![spread_input(&source, declaration(&source))],
        );
    let fixture = run_fixture(opts);
    assert_preserved_entry_output(&fixture, "before\nbase,a,b base,a,b\n");
}

#[test]
fn shadowed_object_cannot_identify_array_iterator_prototype_receiver() {
    let source = format!(
        "function mutate(Object) {{ Object.getPrototypeOf([][Symbol.iterator]()).next = function () {{ return {{ done: true }}; }}; }}\n{SOURCE}"
    );
    let modules = vec![logical_module(
        "scopes",
        &[
            Member::new("clientBefore"),
            Member::new("apiError"),
            Member::new("clientAfter"),
            Member::new("first"),
            Member::new("second"),
            Member::new("scopes"),
        ],
    )];
    let opts = FixtureOpts::new(&source, modules).with_native_operation_inputs(
        "static/app",
        vec![spread_input(&source, declaration(&source))],
    );
    let fixture = run_fixture(opts);
    assert_preserved_entry_output(&fixture, "before\nbase,a,b base,a,b\n");
}

#[test]
fn source_shape_rejects_exposure_holes_and_intervening_effects() {
    for statement in [
        "const first = ['a'], leaked = (globalThis.saved = first), scopes = ['base', ...first];",
        "const first = ['a'], mutated = (first[0] = 'x'), scopes = ['base', ...first];",
        "const first = ['a'], iterator = (first[Symbol.iterator] = function* () {}), scopes = ['base', ...first];",
        "const first = ['a'], accessor = Object.defineProperty(first, '0', { get() { return 'x'; } }), scopes = ['base', ...first];",
        "const first = ['a'], rebound = (first = ['x']), scopes = ['base', ...first];",
        "const first = [,], scopes = ['base', ...first];",
        "const first = ['a'], effect = console.log('between'), scopes = ['base', ...first];",
        "const first = ['a'], alias = first, scopes = ['base', ...first];",
        "const first = ['a'], scopes = ['base', ...first, ...unknown];",
    ] {
        let source = format!("{statement}\nexport {{ scopes }};\n");
        let opts = FixtureOpts::new(
            &source,
            vec![logical_module("scopes", &[Member::new("scopes")])],
        )
        .with_native_operation_inputs("static/app", vec![spread_input(&source, statement)]);
        expect_rejection_containing_all(opts, &["inert same-declaration primitive-array spread"]);
    }
}

#[test]
fn source_digest_must_match_the_selected_parser_input() {
    let changed = SOURCE.replace("[\"a\"]", "[\"changed\"]");
    let opts = FixtureOpts::new(&changed, modules()).with_native_operation_inputs(
        "static/app",
        vec![spread_input(SOURCE, declaration(&changed))],
    );
    expect_rejection_containing_all(opts, &["audited_source_sha256"]);
}

#[test]
fn spread_producers_cannot_be_materialized_in_another_module() {
    let modules = vec![
        logical_module(
            "client",
            &[Member::new("clientBefore"), Member::new("clientAfter")],
        ),
        logical_module("error/schema", &[Member::new("apiError")]),
        logical_module("oauth/first", &[Member::new("first")]),
        logical_module(
            "oauth/scopes",
            &[Member::new("second"), Member::new("scopes")],
        ),
    ];
    let rejected = run_rejection_fixture(FixtureOpts::new(SOURCE, modules));
    let conflicts = rejected.atomic_group_conflicts();
    assert!(
        conflicts.iter().any(|conflict| {
            conflict["claims"].as_array().is_some_and(|claims| {
                ["oauth/first", "oauth/scopes"]
                    .iter()
                    .all(|module| claims.iter().any(|claim| claim["module"] == *module))
            })
        }),
        "producer/consumer split must be rejected: {conflicts:#?}"
    );
}
