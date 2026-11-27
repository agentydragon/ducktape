//! Separately audited factories retain existing and multiple callbacks.

use analysis_model::DepKind;
use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::*;

const PROVIDER: &str =
    "export function factory(callback, compare) { return { invoke: callback, compare }; }\n";
const NAMESPACE_PROVIDER: &str =
    "export const api = { make(callback) { return { invoke: callback }; } };\n";
const CONSUMER: &str = "export function consume(callback) { return Reflect.apply(Reflect.apply(callback, null, []), null, []); }\n";

fn retained_factory(
    argument_count: usize,
    positions: &[usize],
) -> (&'static str, ChunkExportPurity) {
    ChunkExportPurityBuilder::new("static/peer")
        .with_retained_callback_factory("factory", argument_count, positions)
        .build()
}

fn namespace_make_contract() -> (&'static str, ChunkExportPurity) {
    ChunkExportPurityBuilder::new("static/peer")
        .with_retained_callback_factory_member("api", "make", 1, &[0])
        .with_namespace_independent_members("api", &["make"])
        .build()
}

fn gate_diagnose_json(
    fixture: &RejectedFixture,
    binding: &str,
    extra: &[&str],
) -> serde_json::Value {
    let graph = fixture.dependency_graph_path();
    let mut args = vec![
        "gate",
        "diagnose",
        binding,
        "--graph",
        graph.to_str().unwrap(),
        "--modules",
        fixture.modules_root.to_str().unwrap(),
        "--source-root",
        fixture.source_root.to_str().unwrap(),
        "--format",
        "json",
    ];
    args.extend(extra);
    let output = run_debundle(&args);
    process::assert_success(&output);
    parse_stdout_json(&output)
}

/// Runs a witness's generated `inspection_command` and asserts that it shows
/// `snippet` in the witness source unit.
fn assert_inspection_shows(witness: &serde_json::Value, snippet: &str) {
    let command = witness["inspection_command"].as_str().unwrap();
    let inspected = process::output(std::process::Command::new("/bin/sh").arg("-c").arg(command))
        .unwrap_or_else(|err| panic!("{err}"));
    process::assert_success(&inspected);
    let statements = parse_stdout_json(&inspected)["files"][0]["statements"].clone();
    assert!(
        statements.as_array().unwrap().iter().any(|statement| {
            statement["source_unit_ids"]
                .as_array()
                .unwrap()
                .contains(&witness["source_unit"])
                && statement["source"].as_str().unwrap().contains(snippet)
        }),
        "{command} shows no {snippet:?} in {}: {statements}",
        witness["source_unit"]
    );
}

fn assert_eager_read(fixture: &Fixture, source: &str, target: &str) {
    let graph = fixture.dependency_graph();
    let source = source_unit_for_binding(&graph, source);
    let target = source_unit_for_binding(&graph, target);
    assert!(graph.edges.iter().any(|edge| edge.source == source
        && edge.target == target
        && edge.edge_kind == DepKind::EagerUse
        && edge.constrains_init_order));
}

/// A program that runs correctly in one module but whose split between
/// `callbacks/wrapped` and `values/later` must be rejected.
#[derive(Clone, Copy, Default)]
struct SplitCase<'a> {
    name: &'a str,
    source: &'a str,
    chunks: &'a [(&'a str, &'a str)],
    contracts: &'a [(&'a str, ChunkExportPurity)],
    callbacks: &'a [&'static str],
    callback_writes: &'a [&'a str],
    values: &'a [&'static str],
    value_writes: &'a [&'a str],
    output: &'a str,
    /// `(reader, binding)` pairs the colocated graph must record as eager reads.
    eager_reads: &'a [(&'a str, &'a str)],
}

/// Asserts the colocated output and eager reads, rejects the split, and
/// returns the colocated fixture.
fn assert_split_rejected(case: SplitCase<'_>) -> Fixture {
    eprintln!("case: {}", case.name);
    let members =
        |names: &[&'static str]| names.iter().copied().map(Member::new).collect::<Vec<_>>();
    let all = members(&[case.callbacks, case.values].concat());
    let writes = [case.callback_writes, case.value_writes].concat();
    let colocated = run_fixture_with_graphs(
        FixtureOpts::new(
            case.source,
            vec![logical_module_with_anon("all", &all, &writes)],
        )
        .with_extra_chunks(case.chunks)
        .with_chunk_export_purity(case.contracts),
    );
    assert_preserved_entry_output(&colocated, case.output);
    for (reader, binding) in case.eager_reads {
        assert_eager_read(&colocated, reader, binding);
    }
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            case.source,
            vec![
                logical_module_with_anon(
                    "callbacks/wrapped",
                    &members(case.callbacks),
                    case.callback_writes,
                ),
                logical_module_with_anon("values/later", &members(case.values), case.value_writes),
            ],
        )
        .with_extra_chunks(case.chunks)
        .with_chunk_export_purity(case.contracts),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
    colocated
}

#[test]
fn named_local_callback_can_remain_lazy_under_explicit_retention_contract() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { factory as make } from './peer.js';\n\
             const callback = () => later;\nconst wrapped = make(callback);\n\
             const later = 42;\nconsole.log(wrapped.invoke());\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &binding_members(["callback", "wrapped"]),
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", PROVIDER)])
        .with_chunk_export_purity(&[retained_factory(1, &[0])]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/callbacks/wrapped.js",
        &["wrapped"],
        &["later"],
    );
}

#[test]
fn namespace_factory_retains_both_named_and_inline_callback_arguments() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api as localApi } from './peer.js';\n\
             const callback = () => later;\n\
             const wrapped = localApi.make(callback, () => other);\n\
             const later = 42;\nconst other = 7;\n\
             console.log(wrapped.invoke(), wrapped.compare());\n",
            vec![
                logical_module("callbacks/wrapped", &binding_members(["callback", "wrapped"])),
                logical_module("values/later", &[Member::new("later")]),
                logical_module("values/other", &[Member::new("other")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", "export const api = { make(callback, compare) { return { invoke: callback, compare }; } };\n")])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_retained_callback_factory_member("api", "make", 2, &[0, 1])
            .with_callback_factory_members("api", &["make"])
            .with_namespace_independent_members("api", &["make"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42 7\n");
}

#[test]
fn named_declarations_aliases_and_multiple_inline_callbacks_are_admitted() {
    for (name, declarations, call, names) in [
        (
            "function declaration and alias",
            "function callback() { return later; }\nconst alias = callback;\n",
            "make(alias, () => later)",
            vec!["callback", "alias"],
        ),
        (
            "two named arrows",
            "const callback = () => later;\nconst compare = () => later;\n",
            "make(callback, compare)",
            vec!["callback", "compare"],
        ),
        (
            "two inline arrows",
            "",
            "make(() => later, () => later)",
            vec![],
        ),
    ] {
        eprintln!("case: {name}");
        let source = format!(
            "import {{ factory as make }} from './peer.js';\n{declarations}const wrapped = {call};\nconst later = 42;\nconsole.log(wrapped.invoke(), wrapped.compare());\n"
        );
        let mut members: Vec<_> = names.into_iter().map(Member::new).collect();
        members.push(Member::new("wrapped"));
        let fixture = run_fixture(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module("callbacks/wrapped", &members),
                    logical_module("values/later", &[Member::new("later")]),
                ],
            )
            .with_extra_chunks(&[("static/peer", PROVIDER)])
            .with_chunk_export_purity(&[retained_factory(2, &[0, 1])]),
        );
        assert_preserved_entry_output(&fixture, "42 42\n");
    }
}

#[test]
fn inert_metadata_and_retained_results_can_occupy_explicit_positions() {
    let fixture = run_fixture(FixtureOpts::new(
        "import { factory, outer } from './peer.js';\n\
         const callback = () => later;\n\
         const inner = factory(callback);\nconst wrapped = outer('metadata', inner, () => later);\n\
         const later = 42;\nconsole.log(wrapped.inner.invoke(), wrapped.compare());\n",
        vec![logical_module("callbacks/wrapped", &binding_members(["callback", "inner", "wrapped"])),
             logical_module("values/later", &[Member::new("later")])],
    ).with_extra_chunks(&[("static/peer", "export function factory(callback) { return { invoke: callback }; }\nexport function outer(metadata, inner, compare) { return { metadata, inner, compare }; }\n")])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_retained_callback_factory("factory", 1, &[0])
            .with_retained_callback_factory("outer", 3, &[1, 2]).build()]));
    assert_preserved_entry_output(&fixture, "42 42\n");
}

#[test]
fn exposed_or_mutated_factory_results_do_not_certify_later_factory_inputs() {
    for mutation in [
        "install(inner, getter);",
        "Object.defineProperty(inner, 'read', { get() { return { value: getter() }; } });",
        "inner.read = { get value() { return getter(); } };",
        "inner.__proto__ = { get read() { return { value: getter() }; } };",
    ] {
        eprintln!("case: {mutation}");
        let source = format!(
            "import {{ factory, outer, install }} from './peer.js';\n\
             const getter = () => {{ try {{ return later; }} catch (error) {{ return error.name; }} }};\n\
             const inner = factory(() => 1);\n{mutation}\n\
             const wrapped = outer(inner);\nconst later = 42;\nconsole.log(wrapped.value);\n"
        );
        let provider = "export function factory(callback) { return { invoke: callback }; }\n\
                        export function outer(input) { return { value: input.read.value }; }\n\
                        export function install(input, getter) { Object.defineProperty(input, 'read', { get() { return { value: getter() }; } }); }\n";
        let contracts = [ChunkExportPurityBuilder::new("static/peer")
            .with_retained_callback_factory("factory", 1, &[0])
            .with_retained_callback_factory("outer", 1, &[0])
            .build()];
        let colocated = run_fixture(
            FixtureOpts::new(&source, vec![])
                .with_extra_chunks(&[("static/peer", provider)])
                .with_chunk_export_purity(&contracts)
                .with_unassigned_mode(unassigned_mode_inline()),
        );
        assert_preserved_entry_output(&colocated, "ReferenceError\n");
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module_with_anon(
                        "callbacks/wrapped",
                        &binding_members(["getter", "inner", "wrapped"]),
                        &[mutation],
                    ),
                    logical_module("values/later", &[Member::new("later")]),
                ],
            )
            .with_extra_chunks(&[("static/peer", provider)])
            .with_chunk_export_purity(&contracts),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
    }
}

#[test]
fn invalid_signature_positions_report_spec_errors() {
    for (count, positions, message) in [
        (1, vec![], "must list at least one callback argument"),
        (1, vec![1], "position must be less than argument_count"),
    ] {
        eprintln!("case: {message}");
        expect_rejection_containing_all(
            FixtureOpts::new("console.log(0);\n", vec![])
                .with_extra_chunks(&[("static/peer", PROVIDER)])
                .with_chunk_export_purity(&[retained_factory(count, &positions)]),
            &[message],
        );
    }
}

#[test]
fn unsupported_argument_and_callee_shapes_keep_conservative_timing() {
    for (name, declarations, call, names, statements) in [
        (
            "void argument",
            "const callback = () => later;\n",
            "make(callback, void 0)",
            vec!["callback"],
            vec![],
        ),
        (
            "spread arguments",
            "const callback = () => later;\n",
            "make(...[callback, () => later])",
            vec!["callback"],
            vec![],
        ),
        (
            "construct call",
            "const callback = () => later;\n",
            "new make(callback, () => later)",
            vec!["callback"],
            vec![],
        ),
        (
            "local callee alias",
            "const callback = () => later;\nconst alias = make;\n",
            "alias(callback, () => later)",
            vec!["callback", "alias"],
            vec![],
        ),
        (
            "reassigned callback",
            "let callback = () => later;\ncallback = () => later;\n",
            "make(callback, () => later)",
            vec!["callback"],
            vec!["callback = () => later;"],
        ),
        (
            "member callback",
            "const callback = () => later;\nconst object = { callback };\n",
            "make(object.callback, () => later)",
            vec!["callback", "object"],
            vec![],
        ),
        (
            "class callback",
            "class Callback {}\n",
            "make(Callback, () => later)",
            vec!["Callback"],
            vec![],
        ),
        (
            "non-callback extra argument",
            "const callback = () => later;\nconst option = {};\n",
            "make(callback, () => later, option)",
            vec!["callback", "option"],
            vec![],
        ),
    ] {
        eprintln!("case: {name}");
        let source = format!(
            "import {{ factory as make }} from './peer.js';\n{declarations}const wrapped = {call};\nconst later = 42;\nconsole.log(wrapped.invoke());\n"
        );
        let mut members: Vec<_> = names.into_iter().map(Member::new).collect();
        members.push(Member::new("wrapped"));
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module_with_anon("callbacks/wrapped", &members, &statements),
                    logical_module("values/later", &[Member::new("later")]),
                ],
            )
            .with_extra_chunks(&[("static/peer", PROVIDER)])
            .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
                .with_retained_callback_factory("factory", 2, &[0, 1])
                .with_retained_callback_factory("factory", 3, &[0, 1])
                .build()]),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
    }
}

#[test]
fn retained_factory_results_that_reach_callbacks_keep_their_reads() {
    let contracts = [retained_factory(2, &[0, 1])];
    let peer = [("static/peer", PROVIDER)];
    let both_values = SplitCase {
        chunks: &peer,
        contracts: &contracts,
        values: &["later", "other"],
        value_writes: &["later = 42;", "other = 7;"],
        output: "0 42 7\n",
        eager_reads: &[("observed", "later"), ("observed", "other")],
        ..Default::default()
    };
    for (name, projection) in [
        ("extract and invoke first callback", "invoke"),
        ("extract and invoke second callback", "compare"),
    ] {
        let source = format!(
            "import {{ factory }} from './peer.js';\nlet later = 0;\nlet other = 0;\nconst callback = () => later;\nconst wrapped = factory(callback, () => other);\nconst extracted = wrapped.{projection};\nconst observed = extracted();\nlater = 42;\nother = 7;\nconsole.log(observed, later, other);\n"
        );
        assert_split_rejected(SplitCase {
            name,
            source: &source,
            callbacks: &["callback", "wrapped", "extracted", "observed"],
            ..both_values
        });
    }
    assert_split_rejected(SplitCase {
        name: "publishing returned graph exposes both callback positions",
        source: "import { factory } from './peer.js';\nimport { consume } from './host.js';\nlet later = 0;\nlet other = 0;\nconst callback = () => later;\nconst wrapped = factory(callback, () => other);\nconst observed = consume(wrapped);\nlater = 42;\nother = 7;\nconsole.log(observed, later, other);\n",
        chunks: &[
            ("static/peer", PROVIDER),
            (
                "static/host",
                "export function consume(value) { return Reflect.apply(value.compare, null, []); }\n",
            ),
        ],
        callbacks: &["callback", "wrapped", "observed"],
        ..both_values
    });
    assert_split_rejected(SplitCase {
        name: "callback argument evaluation effects remain eager and ordered",
        source: "import { factory } from './peer.js';\nlet later = 0;\nconst callback = () => later;\nconst wrapped = factory((globalThis.seen = callback(), callback), () => later);\nlater = 42;\nconsole.log(globalThis.seen, later);\n",
        chunks: &peer,
        contracts: &contracts,
        callbacks: &["callback", "wrapped"],
        values: &["later"],
        value_writes: &["later = 42;"],
        output: "0 42\n",
        eager_reads: &[("wrapped", "later")],
        ..Default::default()
    });
}

#[test]
fn admitted_call_argument_retains_its_own_synchronous_effects() {
    let colocated = assert_split_rejected(SplitCase {
        name: "admitted call argument",
        source: "import { factory } from './peer.js';\nlet later = 0;\nconst callback = () => later;\nfunction makeReader() { globalThis.seen = callback(); return () => later; }\nconst wrapped = factory(makeReader(), () => other);\nconst other = 7;\nlater = 42;\nconsole.log(globalThis.seen, wrapped.invoke(), wrapped.compare());\n",
        chunks: &[("static/peer", PROVIDER)],
        contracts: &[retained_factory(2, &[0, 1])],
        callbacks: &["callback", "makeReader", "wrapped"],
        values: &["later", "other"],
        value_writes: &["later = 42;"],
        output: "0 42 7\n",
        eager_reads: &[("wrapped", "later")],
        ..Default::default()
    });
    let graph = colocated.dependency_graph();
    let wrapped = source_unit_for_binding(&graph, "wrapped");
    let other = source_unit_for_binding(&graph, "other");
    assert!(!graph.edges.iter().any(|edge| edge.source == wrapped
        && edge.target == other
        && edge.edge_kind == DepKind::EagerUse
        && edge.constrains_init_order));
}

#[test]
fn custom_callable_property_and_prototype_writes_through_aliases_stay_conservative() {
    let contracts = [retained_factory(1, &[0])];
    for (mutation, projection) in [
        ("alias.extra = () => later;", "extra"),
        ("alias.prototype.extra = () => later;", "prototype.extra"),
        (
            "Object.defineProperty(alias, 'extra', { get() { return () => later; } });",
            "extra",
        ),
    ] {
        let source = format!(
            "import {{ factory }} from './peer.js';\nlet later = 0;\nfunction callback() {{ return 1; }}\nconst alias = callback;\n{mutation}\nconst wrapped = factory(callback);\nconst observed = wrapped.invoke.{projection}();\nlater = 42;\nconsole.log(observed, later);\n"
        );
        assert_split_rejected(SplitCase {
            name: mutation,
            source: &source,
            chunks: &[("static/peer", PROVIDER)],
            contracts: &contracts,
            callbacks: &["callback", "alias", "wrapped", "observed"],
            callback_writes: &[mutation],
            values: &["later"],
            value_writes: &["later = 42;"],
            output: "0 42\n",
            ..Default::default()
        });
    }
}

#[test]
fn opaque_calls_that_reach_callbacks_keep_their_reads() {
    let peer = [("static/peer", PROVIDER)];
    let consumer = [("static/peer", CONSUMER)];
    let later = SplitCase {
        values: &["later"],
        value_writes: &["later = 42;"],
        output: "0 42\n",
        ..Default::default()
    };
    assert_split_rejected(SplitCase {
        name: "opaque factory after custom callable property write",
        source: "import { factory } from './peer.js';\nlet later = 0;\nfunction callback() { return 1; }\nconst alias = callback;\nalias.extra = () => later;\nconst wrapped = factory(callback);\nconst observed = wrapped.invoke.extra();\nlater = 42;\nconsole.log(observed, later);\n",
        chunks: &peer,
        callbacks: &["callback", "alias", "wrapped", "observed"],
        callback_writes: &["alias.extra = () => later;"],
        ..later
    });
    assert_split_rejected(SplitCase {
        name: "local helper callable property write",
        source: "import { factory } from './peer.js';\nlet later = 0;\nfunction callback() { return 1; }\nconst alias = callback;\nfunction install() { alias.extra = () => later; }\ninstall();\nconst wrapped = factory(callback);\nconst observed = wrapped.invoke.extra();\nlater = 42;\nconsole.log(observed, later);\n",
        chunks: &peer,
        callbacks: &["callback", "alias", "install", "wrapped", "observed"],
        callback_writes: &["install();"],
        ..later
    });
    assert_split_rejected(SplitCase {
        name: "opaque consumer invokes a returned closure without any assertion",
        source: "import { consume } from './peer.js';\nlet later = 0;\nconst observed = consume(() => () => later);\nlater = 42;\nconsole.log(observed, later);\n",
        chunks: &consumer,
        callbacks: &["observed"],
        ..later
    });
    for (name, expression) in [
        (
            "returned callback roots grow across captures",
            "(consume(() => makeReader(first)), consume(() => makeReader(second)))",
        ),
        (
            "returned callback roots grow across rebinds",
            "(consume(() => makeReader(selected)), selected = second, consume(() => makeReader(selected)))",
        ),
    ] {
        let source = format!(
            "import {{ consume }} from './peer.js';\nlet later = 0;\nfunction makeReader(reader) {{ return () => reader(); }}\nconst first = () => 7;\nconst second = () => later;\nlet selected = first;\nconst result = {expression};\nlater = 42;\nconsole.log(result, later);\n"
        );
        assert_split_rejected(SplitCase {
            name,
            source: &source,
            chunks: &consumer,
            callbacks: &["makeReader", "first", "second", "selected", "result"],
            eager_reads: &[("result", "later")],
            ..later
        });
    }
    assert_split_rejected(SplitCase {
        name: "returned class superclass re-enters returned callback fallback",
        source: "import { consume } from './peer.js';\nlet later = 0;\nfunction Base() { globalThis.observed = consume(() => () => later); }\nclass Derived extends Base {}\nconst result = consume(() => Derived);\nlater = 42;\nconsole.log(globalThis.observed, later);\n",
        chunks: &[(
            "static/peer",
            "export function consume(callback) { const returned = callback(); return returned.prototype ? new returned() : returned(); }\n",
        )],
        callbacks: &["Base", "Derived", "result"],
        eager_reads: &[("result", "later")],
        ..later
    });
}

#[test]
fn returned_callback_identity_cycle_terminates_conservatively() {
    let fixture = run_fixture(FixtureOpts::new(
        "import { consume } from './peer.js';\nfunction callback() { return callback; }\nconst result = consume(callback);\nconst later = 42;\nconsole.log(result === callback, later);\n",
        vec![logical_module("callbacks/wrapped", &binding_members(["callback", "result"])),
             logical_module("values/later", &[Member::new("later")])],
    ).with_extra_chunks(&[("static/peer", "export function consume(callback) { return Reflect.apply(callback, null, []); }\n")]));
    assert_preserved_entry_output(&fixture, "true 42\n");
}

#[test]
fn returned_callback_effects_are_applied_at_each_initialization_prefix() {
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        "import { consume } from './peer.js';\nlet later = 0;\nfunction callback() { return () => later; }\nconst first = consume(callback);\nlater = 42;\nconst second = consume(callback);\nconsole.log(first, second);\n",
        vec![logical_module_with_anon("all", &binding_members(["callback", "first", "second", "later"]), &["later = 42;"])],
    ).with_extra_chunks(&[("static/peer", CONSUMER)]));
    assert_preserved_entry_output(&fixture, "0 42\n");
    assert_eager_read(&fixture, "first", "later");
    assert_eager_read(&fixture, "second", "later");
}

#[test]
fn namespace_member_mutation_disables_retention_contract() {
    for (setup, mutation, names) in [
        ("", "api.make = replacement;", vec![]),
        (
            "const alias = api;\n",
            "alias.make = replacement;",
            vec!["alias"],
        ),
    ] {
        eprintln!("case: {mutation}");
        let source = format!(
            "import {{ api }} from './peer.js';\nlet later = 0;\nconst callback = () => later;\nconst replacement = callback => (globalThis.seen = callback(), {{ invoke: callback }});\n{setup}{mutation}\nconst wrapped = api.make(callback);\nlater = 42;\nconsole.log(globalThis.seen, later);\n"
        );
        let mut members: Vec<_> = names.into_iter().map(Member::new).collect();
        members.extend(binding_members(["callback", "replacement", "wrapped"]));
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module_with_anon("callbacks/wrapped", &members, &[mutation]),
                    logical_module_with_anon(
                        "values/later",
                        &[Member::new("later")],
                        &["later = 42;"],
                    ),
                ],
            )
            .with_extra_chunks(&[("static/peer", NAMESPACE_PROVIDER)])
            .with_chunk_export_purity(&[namespace_make_contract()]),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
    }
}

#[test]
fn diagnose_namespace_member_write_links_contract_dependency_and_inspection() {
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_retained_callback_factory_member("api", "make", 1, &[0])
        .with_retained_callback_factory_member("api", "make", 2, &[0])
        .with_namespace_independent_members("api", &["make"])
        .build()];
    let accepted = run_fixture_with_graphs(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const callback = () => later;\n\
             const wrapped = api.make(callback);\n\
             const later = 42;\n\
             console.log(wrapped.invoke());\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &binding_members(["callback", "wrapped"]),
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_PROVIDER)])
        .with_chunk_export_purity(&contracts),
    );
    assert_preserved_entry_output(&accepted, "42\n");
    assert!(
        accepted
            .dependency_graph()
            .nodes
            .iter()
            .all(|node| node.callback_factory_barriers.is_empty())
    );

    let mut source = String::from(
        "import { api } from './peer.js';\n\
         let later = 0;\n\
         const replacement = callback => (globalThis.seen = callback(), { invoke: callback });\n\
         const alias = api;\n\
         const prefixA = 1, prefixB = 2;\n\
         alias.make = replacement;\n\
         api.make = replacement;\n",
    );
    let mut members = Vec::from(binding_members([
        "replacement",
        "alias",
        "prefixA",
        "prefixB",
    ]));
    source.push_str("const callback0 = () => later;\ncallback0();\n");
    members.push(Member::new("callback0"));
    for (index, (callback, wrapped)) in [
        ("callback1", "wrapped1"),
        ("callback2", "wrapped2"),
        ("callback3", "wrapped3"),
        ("callback4", "wrapped4"),
        ("callback5", "wrapped5"),
        ("callback6", "wrapped6"),
    ]
    .into_iter()
    .enumerate()
    {
        let index = index + 1;
        source.push_str(&format!(
            "const callback{index} = () => later;\nconst wrapped{index} = api.make(callback{index});\n"
        ));
        members.extend([Member::new(callback), Member::new(wrapped)]);
    }
    source.push_str("later = 42;\n");

    let rejected = run_rejection_fixture_with_graphs(
        FixtureOpts::new(
            &source,
            vec![
                logical_module_with_anon(
                    "callbacks/wrapped",
                    &members,
                    &["alias.make = replacement;", "api.make = replacement;"],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_PROVIDER)])
        .with_chunk_export_purity(&contracts),
    );

    let bounded = gate_diagnose_json(&rejected, "later", &[]);
    assert_eq!(bounded["returned_count"], 5);
    assert!(bounded["total_count"].as_u64().unwrap() > 5);
    assert_eq!(bounded["truncated"], true);
    assert_eq!(bounded["expansion"], "bounded");
    assert_eq!(bounded["query_status"], "local_supported_witness_found");
    assert_eq!(
        bounded["coverage_scope"],
        "callback_factory_namespace_writes_and_call_arguments"
    );
    let limited = gate_diagnose_json(&rejected, "later", &["--limit", "1"]);
    assert_eq!(limited["total_count"], bounded["total_count"]);
    assert_eq!(limited["returned_count"], 1);
    assert_eq!(limited["query_status"], "local_supported_witness_found");
    assert_eq!(
        limited["traces"][0]["coverage"], "inconclusive_callback_argument_source_mismatch",
        "the first bounded row is inconclusive even though query status includes supported traces beyond the limit"
    );

    let exhaustive = gate_diagnose_json(&rejected, "later", &["--all"]);
    assert_eq!(exhaustive["returned_count"], exhaustive["total_count"]);
    assert_eq!(exhaustive["truncated"], false);
    assert_eq!(exhaustive["expansion"], "all");
    let first = exhaustive["traces"]
        .as_array()
        .unwrap()
        .iter()
        .find(|trace| trace["coverage"] == "supported_namespace_member_write")
        .expect("a supported trace must back the query's supported status");
    assert_eq!(first["ownership_target"]["binding"], "later");
    assert_eq!(first["ownership_target"]["module"], "values/later");
    let dependency = &first["constraining_dependency"];
    assert_eq!(dependency["edge_kind"], "eager_use");
    assert_eq!(dependency["binding"], "later");
    assert!(
        dependency["callee_source_unit"]
            .as_str()
            .unwrap()
            .starts_with("unit:")
    );
    assert_eq!(dependency["role"], "promoted_at_init");
    assert_eq!(dependency["constrains_init_order"], true);
    let contract = &first["matched_contract"];
    assert_eq!(contract["namespace_binding"], "api");
    assert_eq!(contract["member"], "make");
    assert_eq!(contract["argument_count"], 1);
    assert_eq!(
        contract["callback_source_units"].as_array().unwrap().len(),
        1
    );
    assert_eq!(contract["callback_arguments"], serde_json::json!([0]));
    let guard = &first["first_rejected_guard"];
    assert_eq!(guard["code"], "namespace_member_write");
    let witness = &guard["witness"];
    assert_eq!(witness["receiver_binding"], "alias");
    assert_eq!(witness["property"], "make");
    assert_eq!(
        witness["witness_selection"],
        "first_source_unit_order_deterministic_within_unit"
    );
    let location = &witness["source_location"];
    assert_eq!(location["source_path"], "static/app.js");
    assert!(location["start_line"].as_u64().unwrap() > 0);
    assert!(location["end_line"].as_u64().unwrap() >= location["start_line"].as_u64().unwrap());
    let inspection = witness["inspection_command"].as_str().unwrap();
    let executable = debundler_path()
        .canonicalize()
        .expect("resolve debundle runfile executable");
    let argv = shlex::split(inspection).expect("inspection command is a POSIX shell command");
    assert_eq!(
        argv[0],
        executable.to_str().unwrap(),
        "inspection must invoke the same debundle executable: {inspection}"
    );
    for flag in ["inspect-source", "--graph", "--modules", "--source-root"] {
        assert!(
            argv.iter().any(|arg| arg == flag),
            "missing {flag}: {inspection}"
        );
    }
    for path in [
        rejected.report_path("dependency_graph.json.gz"),
        rejected.modules_root.clone(),
        rejected.source_root.clone(),
    ] {
        let path = path.canonicalize().unwrap();
        assert!(
            inspection.contains(path.to_str().unwrap()),
            "missing {path:?}: {inspection}"
        );
    }
    assert_inspection_shows(witness, "alias.make = replacement");
}

#[test]
fn same_source_unit_unrelated_callback_is_not_attributed_to_factory_barrier() {
    let rejected = run_rejection_fixture_with_graphs(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             let later = 0;\n\
             let other = 0;\n\
             const replacement = callback => ({ invoke: callback });\n\
             api.make = replacement;\n\
             const callbackA = () => later;\n\
             const callbackB = () => other;\n\
             const result = (api.make(callbackA), unknown(callbackB));\n\
             later = 42;\n\
             other = 7;\n",
            vec![
                logical_module_with_anon(
                    "callbacks/wrapped",
                    &binding_members(["replacement", "callbackA", "callbackB", "result"]),
                    &["api.make = replacement;"],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
                logical_module_with_anon("values/other", &[Member::new("other")], &["other = 7;"]),
            ],
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_PROVIDER)])
        .with_chunk_export_purity(&[namespace_make_contract()]),
    );

    let report = gate_diagnose_json(&rejected, "other", &["--all"]);
    assert!(report["total_count"].as_u64().unwrap() > 0);
    let trace = &report["traces"][0];
    assert_eq!(trace["ownership_target"]["binding"], "other");
    assert_eq!(
        trace["coverage"],
        "inconclusive_callback_argument_source_mismatch"
    );
    assert!(trace["matched_contract"].is_null());
    assert_eq!(
        trace["first_rejected_guard"]["code"],
        "callback_argument_source_mismatch"
    );
}

/// Rejects `source` split between `callbacks/wrapped` (with `members` and
/// `anonymous` statements) and `values/later` under the `api.make` contract.
fn rejected_namespace_fixture(
    source: &str,
    provider: &str,
    members: &[Member],
    anonymous: &[&str],
) -> RejectedFixture {
    run_rejection_fixture_with_graphs(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon("callbacks/wrapped", members, anonymous),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&[("static/peer", provider)])
        .with_chunk_export_purity(&[namespace_make_contract()]),
    )
}

#[test]
fn dynamic_namespace_write_reports_analyzer_witness_without_changing_gate() {
    let rejected = rejected_namespace_fixture(
        "import { api } from './peer.js';\n\
         let later = 0;\n\
         const callback = () => later;\n\
         const replacement = callback => ({ invoke: callback });\n\
         const method = 'make';\n\
         api[method] = replacement;\n\
         const wrapped = api.make(callback);\n\
         later = 42;\n",
        NAMESPACE_PROVIDER,
        &binding_members(["callback", "replacement", "method", "wrapped"]),
        &["api[method] = replacement;"],
    );

    let report = gate_diagnose_json(&rejected, "later", &[]);
    assert!(report["total_count"].as_u64().unwrap() > 0);
    assert_eq!(report["query_status"], "local_supported_witness_found");
    assert_eq!(
        report["coverage_scope"],
        "callback_factory_namespace_writes_and_call_arguments"
    );
    let trace = &report["traces"][0];
    assert_eq!(
        trace["coverage"],
        "supported_namespace_dynamic_property_write"
    );
    assert_eq!(trace["matched_contract"]["member"], "make");
    assert_eq!(
        trace["first_rejected_guard"]["code"],
        "namespace_dynamic_property_write"
    );
    assert_eq!(trace["constraining_dependency"]["binding"], "later");
    assert_eq!(trace["constraining_dependency"]["role"], "promoted_at_init");
    assert_eq!(
        trace["matched_contract"]["callback_arguments"],
        serde_json::json!([0])
    );
    let witness = &trace["first_rejected_guard"]["witness"];
    assert_eq!(witness["receiver_binding"], "api");
    assert!(witness["property"].is_null());
    assert_inspection_shows(witness, "api[method] = replacement");
}

#[test]
fn computed_write_through_aggregate_receiver_remains_inconclusive() {
    let rejected = rejected_namespace_fixture(
        "import { api } from './peer.js';\n\
         let later = 0;\n\
         const holder = { api };\n\
         const method = 'make';\n\
         holder.api[method] = callback => ({ invoke: callback });\n\
         const callback = () => later;\n\
         const wrapped = api.make(callback);\n\
         later = 42;\n",
        NAMESPACE_PROVIDER,
        &binding_members(["holder", "method", "callback", "wrapped"]),
        &["holder.api[method] = callback => ({ invoke: callback });"],
    );
    let report = gate_diagnose_json(&rejected, "later", &[]);
    assert_eq!(
        report["query_status"],
        "inconclusive_for_supported_guard_family"
    );
    assert_eq!(
        report["traces"][0]["coverage"],
        "inconclusive_unsupported_guard"
    );
    assert_eq!(
        report["traces"][0]["first_rejected_guard"]["code"],
        "unsupported_guard"
    );
}

#[test]
fn direct_namespace_call_argument_has_exact_callback_barrier_witness() {
    let rejected = rejected_namespace_fixture(
        "import { api } from './peer.js';\n\
         let later = 0;\n\
         const callback = () => later;\n\
         sink(api);\n\
         const wrapped = api.make(callback);\n\
         later = 42;\n",
        NAMESPACE_PROVIDER,
        &binding_members(["callback", "wrapped"]),
        &["sink(api);"],
    );
    let report = gate_diagnose_json(&rejected, "later", &[]);
    assert_eq!(
        report["query_status"], "local_supported_witness_found",
        "{report}"
    );
    let trace = &report["traces"][0];
    assert_eq!(
        trace["coverage"], "supported_namespace_call_argument",
        "{report}"
    );
    assert_eq!(trace["matched_contract"]["member"], "make");
    assert_eq!(
        trace["first_rejected_guard"]["code"],
        "namespace_call_argument"
    );
    let witness = &trace["first_rejected_guard"]["witness"];
    assert_eq!(witness["argument_index"], 0);
    assert_eq!(witness["namespace_binding"], "api");
    assert_eq!(witness["opaque_call"], true);
    assert!(
        witness["call_span_start"].as_u64().unwrap() < witness["call_span_end"].as_u64().unwrap()
    );
    assert_inspection_shows(witness, "sink(api)");
}

fn rejected_namespace_argument_variant(
    call_source: &str,
    factory_call: &str,
    extra_members: &[&'static str],
    anonymous_statements: &[&str],
) -> RejectedFixture {
    let source = format!(
        "import {{ api, other }} from './peer.js';\n\
         let later = 0;\n\
         const callback = () => later;\n\
         function sink(value) {{ return 0; }}\n\
         {call_source}\n\
         const wrapped = {factory_call};\n\
         later = 42;\n"
    );
    let mut members = Vec::from(binding_members(["sink", "callback", "wrapped"]));
    members.extend(extra_members.iter().copied().map(Member::new));
    rejected_namespace_fixture(
        &source,
        "export const api = { make(callback) { return { invoke: callback }; } };\nexport const other = {};\n",
        &members,
        anonymous_statements,
    )
}

#[test]
fn known_namespace_call_argument_is_not_labeled_opaque_by_unrelated_loss() {
    for (call_source, anonymous) in [
        ("sink(api);", "sink(api);"),
        (
            "(sink(api), unknown(other));",
            "(sink(api), unknown(other));",
        ),
    ] {
        eprintln!("case: {call_source}");
        let rejected = rejected_namespace_argument_variant(
            call_source,
            "api.make(callback)",
            &[],
            &[anonymous],
        );
        let report = gate_diagnose_json(&rejected, "later", &[]);
        assert_eq!(
            report["query_status"], "local_supported_witness_found",
            "{report}"
        );
        let witness = &report["traces"][0]["first_rejected_guard"]["witness"];
        assert_eq!(
            report["traces"][0]["coverage"], "supported_namespace_call_argument",
            "{report}"
        );
        assert_eq!(witness["opaque_call"], false, "{report}");
        assert_eq!(witness["argument_index"], 0);
    }
}

#[test]
fn alias_aggregate_signature_and_ambiguous_calls_stay_inconclusive() {
    for (call_source, factory_call, extra_members, anonymous) in [
        (
            "const alias = api; sink(alias);",
            "api.make(callback)",
            vec!["alias"],
            vec!["sink(alias);"],
        ),
        (
            "sink({ api });",
            "api.make(callback)",
            vec![],
            vec!["sink({ api });"],
        ),
        (
            "sink?.(api);",
            "api.make(callback)",
            vec![],
            vec!["sink?.(api);"],
        ),
        (
            "sink(...[api]);",
            "api.make(callback)",
            vec![],
            vec!["sink(...[api]);"],
        ),
        (
            "sink(api);",
            "api.make(callback, 0)",
            vec![],
            vec!["sink(api);"],
        ),
        (
            "sink(api);",
            "(api.make(callback), api.make(callback))",
            vec![],
            vec!["sink(api);"],
        ),
    ] {
        eprintln!("case: {call_source} {factory_call}");
        let rejected = rejected_namespace_argument_variant(
            call_source,
            factory_call,
            &extra_members,
            &anonymous,
        );
        let report = gate_diagnose_json(&rejected, "later", &[]);
        assert_ne!(
            report["query_status"], "local_supported_witness_found",
            "{report}"
        );
        assert!(
            report["traces"]
                .as_array()
                .unwrap()
                .iter()
                .all(|trace| trace["coverage"] != "supported_namespace_call_argument"),
            "{report}"
        );
    }
}

#[test]
fn old_and_new_contracts_are_independent_and_unmatched_shapes_fall_back() {
    for (call, accepted) in [
        ("factory(() => later)", true),
        ("factory(callback, () => later)", true),
        ("factory(callback)", false),
    ] {
        eprintln!("case: {call}");
        let source = format!(
            "import {{ factory }} from './peer.js';\nconst callback = () => later;\nconst wrapped = {call};\nconst later = 42;\nconsole.log(wrapped.invoke());\n"
        );
        let modules = vec![
            logical_module(
                "callbacks/wrapped",
                &binding_members(["callback", "wrapped"]),
            ),
            logical_module("values/later", &[Member::new("later")]),
        ];
        let contracts = [ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factories(&["factory"])
            .with_retained_callback_factory("factory", 2, &[0, 1])
            .build()];
        let opts = FixtureOpts::new(&source, modules)
            .with_extra_chunks(&[("static/peer", PROVIDER)])
            .with_chunk_export_purity(&contracts);
        if accepted {
            assert_preserved_entry_output(&run_fixture(opts), "42\n");
        } else {
            expect_atomic_conflict_rejection(opts, &["callbacks/wrapped", "values/later"], &[]);
        }
    }
}

#[test]
fn previously_published_callback_keeps_conservative_timing() {
    expect_atomic_conflict_rejection(FixtureOpts::new(
        "import { factory } from './peer.js';\nconst callback = () => later;\nglobalThis.saved = callback;\nconst wrapped = factory(callback);\nconst later = 42;\nconsole.log(wrapped.invoke());\n",
        vec![logical_module_with_anon("callbacks/wrapped", &binding_members(["callback", "wrapped"]), &["globalThis.saved = callback;"]),
             logical_module("values/later", &[Member::new("later")])],
    ).with_extra_chunks(&[("static/peer", PROVIDER)]).with_chunk_export_purity(&[retained_factory(1, &[0])]), &["callbacks/wrapped", "values/later"], &[]);
}

#[test]
fn distinct_local_closure_instances_preserve_their_captured_callbacks() {
    let fixture = run_fixture(FixtureOpts::new(
        "import { factory } from './peer.js';\nfunction reader(callback) { return () => callback(); }\nconst first = reader(() => later);\nconst second = reader(() => other);\nconst wrapped = factory(first, second);\nconst later = 42;\nconst other = 7;\nconsole.log(wrapped.invoke(), wrapped.compare());\n",
        vec![logical_module("callbacks/wrapped", &binding_members(["reader", "first", "second", "wrapped"])),
             logical_module("values/later", &[Member::new("later")]), logical_module("values/other", &[Member::new("other")])],
    ).with_extra_chunks(&[("static/peer", PROVIDER)]).with_chunk_export_purity(&[retained_factory(2, &[0, 1])]));
    assert_preserved_entry_output(&fixture, "42 7\n");
}
