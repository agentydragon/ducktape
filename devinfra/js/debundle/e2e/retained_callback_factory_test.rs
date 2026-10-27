//! Separately audited factories retain existing and multiple callbacks.

use analysis::DepKind;
use debundle_e2e_support::*;

const PROVIDER: &str =
    "export function factory(callback, compare) { return { invoke: callback, compare }; }\n";

fn gate_diagnose_json(
    fixture: &RejectedFixture,
    binding: &str,
    extra: &[&str],
) -> serde_json::Value {
    let graph = fixture
        .report_root
        .join(&fixture.chunk_id)
        .join("dependency_graph.json")
        .to_string_lossy()
        .into_owned();
    let modules = fixture.modules_root.to_string_lossy().into_owned();
    let source_root = fixture.source_root.to_string_lossy().into_owned();
    let mut args = vec![
        "gate".to_string(),
        "diagnose".to_string(),
        binding.to_string(),
        "--graph".to_string(),
        graph,
        "--modules".to_string(),
        modules,
        "--source-root".to_string(),
        source_root,
        "--format".to_string(),
        "json".to_string(),
    ];
    args.extend(extra.iter().map(|arg| (*arg).to_string()));
    let args = args.iter().map(String::as_str).collect::<Vec<_>>();
    let output = run_debundle(&args);
    assert!(
        output.status.success(),
        "gate diagnose failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr),
    );
    parse_stdout_json(&output)
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
                    &[Member::new("callback"), Member::new("wrapped")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", PROVIDER)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_retained_callback_factory("factory", 1, &[0])
            .build()]),
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
                logical_module("callbacks/wrapped", &[Member::new("callback"), Member::new("wrapped")]),
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
    for (declarations, call, names) in [
        (
            "function callback() { return later; }\nconst alias = callback;\n",
            "make(alias, () => later)",
            vec!["callback", "alias"],
        ),
        (
            "const callback = () => later;\nconst compare = () => later;\n",
            "make(callback, compare)",
            vec!["callback", "compare"],
        ),
        ("", "make(() => later, () => later)", vec![]),
    ] {
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
            .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
                .with_retained_callback_factory("factory", 2, &[0, 1])
                .build()]),
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
        vec![logical_module("callbacks/wrapped", &[Member::new("callback"), Member::new("inner"), Member::new("wrapped")]),
             logical_module("values/later", &[Member::new("later")])],
    ).with_extra_chunks(&[("static/peer", "export function factory(callback) { return { invoke: callback }; }\nexport function outer(metadata, inner, compare) { return { metadata, inner, compare }; }\n")])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_retained_callback_factory("factory", 1, &[0])
            .with_retained_callback_factory("outer", 3, &[1, 2]).build()]));
    assert_preserved_entry_output(&fixture, "42 42\n");
}

#[test]
fn invalid_signature_positions_report_spec_errors() {
    for (count, positions, message) in [
        (1, vec![], "must list at least one callback argument"),
        (1, vec![1], "position must be less than argument_count"),
    ] {
        expect_rejection_containing_all(
            FixtureOpts::new("console.log(0);\n", vec![])
                .with_extra_chunks(&[("static/peer", PROVIDER)])
                .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
                    .with_retained_callback_factory("factory", count, &positions)
                    .build()]),
            &[message],
        );
    }
}

#[test]
fn unsupported_argument_and_callee_shapes_keep_conservative_timing() {
    for (declarations, call, names, statements) in [
        (
            "const callback = () => later;\n",
            "make(callback, void 0)",
            vec!["callback"],
            vec![],
        ),
        (
            "const callback = () => later;\n",
            "make(...[callback, () => later])",
            vec!["callback"],
            vec![],
        ),
        (
            "const callback = () => later;\n",
            "new make(callback, () => later)",
            vec!["callback"],
            vec![],
        ),
        (
            "const callback = () => later;\nconst alias = make;\n",
            "alias(callback, () => later)",
            vec!["callback", "alias"],
            vec![],
        ),
        (
            "let callback = () => later;\ncallback = () => later;\n",
            "make(callback, () => later)",
            vec!["callback"],
            vec!["callback = () => later;"],
        ),
        (
            "const callback = () => later;\nconst object = { callback };\n",
            "make(object.callback, () => later)",
            vec!["callback", "object"],
            vec![],
        ),
        (
            "class Callback {}\n",
            "make(Callback, () => later)",
            vec!["Callback"],
            vec![],
        ),
        (
            "const callback = () => later;\nconst option = {};\n",
            "make(callback, () => later, option)",
            vec!["callback", "option"],
            vec![],
        ),
    ] {
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
fn extracting_and_invoking_either_returned_callback_keeps_all_reads() {
    for projection in ["invoke", "compare"] {
        let source = format!(
            "import {{ factory }} from './peer.js';\nlet later = 0;\nlet other = 0;\nconst callback = () => later;\nconst wrapped = factory(callback, () => other);\nconst extracted = wrapped.{projection};\nconst observed = extracted();\nlater = 42;\nother = 7;\nconsole.log(observed, later, other);\n"
        );
        let contracts = [ChunkExportPurityBuilder::new("static/peer")
            .with_retained_callback_factory("factory", 2, &[0, 1])
            .build()];
        let callbacks = [
            Member::new("callback"),
            Member::new("wrapped"),
            Member::new("extracted"),
            Member::new("observed"),
        ];
        let all: Vec<_> = [
            "callback",
            "wrapped",
            "extracted",
            "observed",
            "later",
            "other",
        ]
        .into_iter()
        .map(Member::new)
        .collect();
        let colocated = run_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module_with_anon(
                    "all",
                    &all,
                    &["later = 42;", "other = 7;"],
                )],
            )
            .with_extra_chunks(&[("static/peer", PROVIDER)])
            .with_chunk_export_purity(&contracts),
        );
        assert_preserved_entry_output(&colocated, "0 42 7\n");
        assert_eager_read(&colocated, "observed", "later");
        assert_eager_read(&colocated, "observed", "other");
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module("callbacks/wrapped", &callbacks),
                    logical_module_with_anon(
                        "values/later",
                        &[Member::new("later"), Member::new("other")],
                        &["later = 42;", "other = 7;"],
                    ),
                ],
            )
            .with_extra_chunks(&[("static/peer", PROVIDER)])
            .with_chunk_export_purity(&contracts),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
    }
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

#[test]
fn custom_callable_property_and_prototype_writes_through_aliases_stay_conservative() {
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
        let contracts = [ChunkExportPurityBuilder::new("static/peer")
            .with_retained_callback_factory("factory", 1, &[0])
            .build()];
        let all: Vec<_> = ["callback", "alias", "wrapped", "observed", "later"]
            .into_iter()
            .map(Member::new)
            .collect();
        let colocated = run_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module_with_anon(
                    "all",
                    &all,
                    &[mutation, "later = 42;"],
                )],
            )
            .with_extra_chunks(&[("static/peer", PROVIDER)])
            .with_chunk_export_purity(&contracts),
        );
        assert_preserved_entry_output(&colocated, "0 42\n");
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module_with_anon(
                        "callbacks/wrapped",
                        &[
                            Member::new("callback"),
                            Member::new("alias"),
                            Member::new("wrapped"),
                            Member::new("observed"),
                        ],
                        &[mutation],
                    ),
                    logical_module_with_anon(
                        "values/later",
                        &[Member::new("later")],
                        &["later = 42;"],
                    ),
                ],
            )
            .with_extra_chunks(&[("static/peer", PROVIDER)])
            .with_chunk_export_purity(&contracts),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
    }
}

#[test]
fn opaque_factory_after_custom_callable_property_write_keeps_callback_reads() {
    assert_callable_surface_split(
        "import { factory } from './peer.js';\nlet later = 0;\nfunction callback() { return 1; }\nconst alias = callback;\nalias.extra = () => later;\nconst wrapped = factory(callback);\nconst observed = wrapped.invoke.extra();\nlater = 42;\nconsole.log(observed, later);\n",
        &["callback", "alias", "wrapped", "observed"],
        &["alias.extra = () => later;"],
    );
}

#[test]
fn local_helper_callable_property_write_keeps_callback_reads() {
    assert_callable_surface_split(
        "import { factory } from './peer.js';\nlet later = 0;\nfunction callback() { return 1; }\nconst alias = callback;\nfunction install() { alias.extra = () => later; }\ninstall();\nconst wrapped = factory(callback);\nconst observed = wrapped.invoke.extra();\nlater = 42;\nconsole.log(observed, later);\n",
        &["callback", "alias", "install", "wrapped", "observed"],
        &["install();"],
    );
}

#[test]
fn opaque_consumer_can_invoke_a_returned_closure_without_any_assertion() {
    let source = "import { consume } from './peer.js';\nlet later = 0;\nconst observed = consume(() => () => later);\nlater = 42;\nconsole.log(observed, later);\n";
    let chunks = [(
        "static/peer",
        "export function consume(callback) { return Reflect.apply(Reflect.apply(callback, null, []), null, []); }\n",
    )];
    let colocated = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "all",
                &[Member::new("observed"), Member::new("later")],
                &["later = 42;"],
            )],
        )
        .with_extra_chunks(&chunks),
    );
    assert_preserved_entry_output(&colocated, "0 42\n");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("callbacks/wrapped", &[Member::new("observed")]),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&chunks),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn returned_callback_identity_cycle_terminates_conservatively() {
    let fixture = run_fixture(FixtureOpts::new(
        "import { consume } from './peer.js';\nfunction callback() { return callback; }\nconst result = consume(callback);\nconst later = 42;\nconsole.log(result === callback, later);\n",
        vec![logical_module("callbacks/wrapped", &[Member::new("callback"), Member::new("result")]),
             logical_module("values/later", &[Member::new("later")])],
    ).with_extra_chunks(&[("static/peer", "export function consume(callback) { return Reflect.apply(callback, null, []); }\n")]));
    assert_preserved_entry_output(&fixture, "true 42\n");
}

#[test]
fn returned_callback_roots_grow_across_captures_and_rebinds_in_one_initializer() {
    for expression in [
        "(consume(() => makeReader(first)), consume(() => makeReader(second)))",
        "(consume(() => makeReader(selected)), selected = second, consume(() => makeReader(selected)))",
    ] {
        let source = format!(
            "import {{ consume }} from './peer.js';\nlet later = 0;\nfunction makeReader(reader) {{ return () => reader(); }}\nconst first = () => 7;\nconst second = () => later;\nlet selected = first;\nconst result = {expression};\nlater = 42;\nconsole.log(result, later);\n"
        );
        let names = ["makeReader", "first", "second", "selected", "result"];
        let all: Vec<_> = names
            .into_iter()
            .chain(["later"])
            .map(Member::new)
            .collect();
        let callbacks: Vec<_> = names.into_iter().map(Member::new).collect();
        let chunks = [(
            "static/peer",
            "export function consume(callback) { return Reflect.apply(Reflect.apply(callback, null, []), null, []); }\n",
        )];
        let colocated = run_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module_with_anon("all", &all, &["later = 42;"])],
            )
            .with_extra_chunks(&chunks),
        );
        assert_preserved_entry_output(&colocated, "0 42\n");
        assert_eager_read(&colocated, "result", "later");
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module("callbacks/wrapped", &callbacks),
                    logical_module_with_anon(
                        "values/later",
                        &[Member::new("later")],
                        &["later = 42;"],
                    ),
                ],
            )
            .with_extra_chunks(&chunks),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
    }
}

#[test]
fn returned_class_superclass_can_reenter_returned_callback_fallback() {
    let source = "import { consume } from './peer.js';\nlet later = 0;\nfunction Base() { globalThis.observed = consume(() => () => later); }\nclass Derived extends Base {}\nconst result = consume(() => Derived);\nlater = 42;\nconsole.log(globalThis.observed, later);\n";
    let chunks = [(
        "static/peer",
        "export function consume(callback) { const returned = callback(); return returned.prototype ? new returned() : returned(); }\n",
    )];
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "all",
                &[
                    Member::new("Base"),
                    Member::new("Derived"),
                    Member::new("result"),
                    Member::new("later"),
                ],
                &["later = 42;"],
            )],
        )
        .with_extra_chunks(&chunks),
    );
    assert_preserved_entry_output(&fixture, "0 42\n");
    assert_eager_read(&fixture, "result", "later");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[
                        Member::new("Base"),
                        Member::new("Derived"),
                        Member::new("result"),
                    ],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&chunks),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn returned_callback_effects_are_applied_at_each_initialization_prefix() {
    let fixture = run_fixture(FixtureOpts::new(
        "import { consume } from './peer.js';\nlet later = 0;\nfunction callback() { return () => later; }\nconst first = consume(callback);\nlater = 42;\nconst second = consume(callback);\nconsole.log(first, second);\n",
        vec![logical_module_with_anon("all", &[Member::new("callback"), Member::new("first"), Member::new("second"), Member::new("later")], &["later = 42;"])],
    ).with_extra_chunks(&[("static/peer", "export function consume(callback) { return Reflect.apply(Reflect.apply(callback, null, []), null, []); }\n")]));
    assert_preserved_entry_output(&fixture, "0 42\n");
    assert_eager_read(&fixture, "first", "later");
    assert_eager_read(&fixture, "second", "later");
}

fn assert_callable_surface_split(source: &str, names: &[&'static str], writes: &[&str]) {
    let all: Vec<_> = names
        .iter()
        .copied()
        .chain(["later"])
        .map(Member::new)
        .collect();
    let callbacks: Vec<_> = names.iter().copied().map(Member::new).collect();
    let mut all_statements = writes.to_vec();
    all_statements.push("later = 42;");
    let colocated = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon("all", &all, &all_statements)],
        )
        .with_extra_chunks(&[("static/peer", PROVIDER)]),
    );
    assert_preserved_entry_output(&colocated, "0 42\n");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon("callbacks/wrapped", &callbacks, writes),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&[("static/peer", PROVIDER)]),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
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
        let source = format!(
            "import {{ api }} from './peer.js';\nlet later = 0;\nconst callback = () => later;\nconst replacement = callback => (globalThis.seen = callback(), {{ invoke: callback }});\n{setup}{mutation}\nconst wrapped = api.make(callback);\nlater = 42;\nconsole.log(globalThis.seen, later);\n"
        );
        let mut members: Vec<_> = names.into_iter().map(Member::new).collect();
        members.extend([
            Member::new("callback"),
            Member::new("replacement"),
            Member::new("wrapped"),
        ]);
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
            .with_extra_chunks(&[(
                "static/peer",
                "export const api = { make(callback) { return { invoke: callback }; } };\n",
            )])
            .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
                .with_retained_callback_factory_member("api", "make", 1, &[0])
                .with_namespace_independent_members("api", &["make"])
                .build()]),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
    }
}

#[test]
fn diagnose_namespace_member_write_links_contract_dependency_and_inspection() {
    let accepted = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const callback = () => later;\n\
             const wrapped = api.make(callback);\n\
             const later = 42;\n\
             console.log(wrapped.invoke());\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("callback"), Member::new("wrapped")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: callback }; } };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_retained_callback_factory_member("api", "make", 1, &[0])
            .with_retained_callback_factory_member("api", "make", 2, &[0])
            .with_namespace_independent_members("api", &["make"])
            .build()]),
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
    let mut members = vec![
        Member::new("replacement"),
        Member::new("alias"),
        Member::new("prefixA"),
        Member::new("prefixB"),
    ];
    source.push_str("const callback0 = () => later;\ncallback0();\n");
    members.push(Member::new("callback0"));
    for index in 1..=6 {
        source.push_str(&format!(
            "const callback{index} = () => later;\nconst wrapped{index} = api.make(callback{index});\n"
        ));
        members.push(Member::new(match index {
            1 => "callback1",
            2 => "callback2",
            3 => "callback3",
            4 => "callback4",
            5 => "callback5",
            6 => "callback6",
            _ => unreachable!(),
        }));
        members.push(Member::new(match index {
            1 => "wrapped1",
            2 => "wrapped2",
            3 => "wrapped3",
            4 => "wrapped4",
            5 => "wrapped5",
            6 => "wrapped6",
            _ => unreachable!(),
        }));
    }
    source.push_str("later = 42;\n");

    let rejected = run_rejection_fixture(
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
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: callback }; } };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_retained_callback_factory_member("api", "make", 1, &[0])
            .with_retained_callback_factory_member("api", "make", 2, &[0])
            .with_namespace_independent_members("api", &["make"])
            .build()]),
    );

    let bounded = gate_diagnose_json(&rejected, "later", &[]);
    assert_eq!(bounded["returned_count"], 5);
    assert!(bounded["total_count"].as_u64().unwrap() > 5);
    assert_eq!(bounded["truncated"], true);
    assert_eq!(bounded["expansion"], "bounded");
    assert_eq!(bounded["query_status"], "local_supported_witness_found");
    assert_eq!(
        bounded["coverage_scope"],
        "callback_factory_namespace_member_write_guard_only"
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
    assert_eq!(first["constraining_dependency"]["edge_kind"], "eager_use");
    assert_eq!(first["constraining_dependency"]["binding"], "later");
    assert!(
        first["constraining_dependency"]["callee_source_unit"]
            .as_str()
            .unwrap()
            .starts_with("unit:")
    );
    assert_eq!(first["constraining_dependency"]["role"], "promoted_at_init");
    assert_eq!(
        first["constraining_dependency"]["constrains_init_order"],
        true
    );
    assert_eq!(first["coverage"], "supported_namespace_member_write");
    assert_eq!(first["matched_contract"]["namespace_binding"], "api");
    assert_eq!(first["matched_contract"]["member"], "make");
    assert_eq!(first["matched_contract"]["argument_count"], 1);
    assert_eq!(
        first["matched_contract"]["callback_source_units"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    assert_eq!(
        first["matched_contract"]["callback_arguments"],
        serde_json::json!([0])
    );
    let guard = &first["first_rejected_guard"];
    assert_eq!(guard["code"], "namespace_member_write");
    let witness = &guard["witness"];
    assert_eq!(witness["receiver_binding"], "alias");
    assert_eq!(witness["property"], "make");
    assert_eq!(
        witness["witness_selection"],
        "first_source_unit_order_deterministic_within_unit"
    );
    assert_eq!(witness["source_location"]["source_path"], "static/app.js");
    assert!(witness["source_location"]["start_line"].as_u64().unwrap() > 0);
    assert!(
        witness["source_location"]["end_line"].as_u64().unwrap()
            >= witness["source_location"]["start_line"].as_u64().unwrap()
    );
    let inspection = witness["inspection_command"].as_str().unwrap();
    let binary = debundler_path();
    let executable = binary
        .canonicalize()
        .expect("resolve debundle runfile executable");
    let expected_program = format!(
        "'{}' inspect-source",
        executable.to_string_lossy().replace('\'', "'\\''")
    );
    assert!(
        inspection.starts_with(&expected_program),
        "inspection must invoke the same debundle executable: {inspection}"
    );
    assert!(inspection.contains("--graph '"));
    assert!(inspection.contains("--modules '"));
    assert!(inspection.contains("--source-root '"));
    assert!(
        inspection.contains(
            rejected
                .report_root
                .join(&rejected.chunk_id)
                .join("dependency_graph.json")
                .canonicalize()
                .unwrap()
                .to_str()
                .unwrap()
        )
    );
    assert!(
        inspection.contains(
            rejected
                .modules_root
                .canonicalize()
                .unwrap()
                .to_str()
                .unwrap()
        )
    );
    assert!(
        inspection.contains(
            rejected
                .source_root
                .canonicalize()
                .unwrap()
                .to_str()
                .unwrap()
        )
    );

    // Execute the exact generated command with the runfile binary directory on PATH.
    let binary_dir = binary.parent().expect("debundle runfile has a parent");
    let path = format!(
        "{}:{}",
        binary_dir.display(),
        std::env::var("PATH").unwrap_or_default()
    );
    let inspected = std::process::Command::new("/bin/sh")
        .arg("-c")
        .arg(inspection)
        .env("PATH", path)
        .output()
        .expect("execute generated inspection command");
    assert!(
        inspected.status.success(),
        "generated inspection command failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&inspected.stdout),
        String::from_utf8_lossy(&inspected.stderr),
    );
    let inspected = parse_stdout_json(&inspected);
    assert!(
        inspected["files"][0]["statements"]
            .as_array()
            .unwrap()
            .iter()
            .any(|statement| {
                statement["source_unit_ids"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|id| id == &witness["source_unit"])
                    && statement["source"]
                        .as_str()
                        .unwrap()
                        .contains("alias.make = replacement")
            })
    );
}

#[test]
fn same_source_unit_unrelated_callback_is_not_attributed_to_factory_barrier() {
    let rejected = run_rejection_fixture(
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
                    &[
                        Member::new("replacement"),
                        Member::new("callbackA"),
                        Member::new("callbackB"),
                        Member::new("result"),
                    ],
                    &["api.make = replacement;"],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
                logical_module_with_anon("values/other", &[Member::new("other")], &["other = 7;"]),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: callback }; } };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_retained_callback_factory_member("api", "make", 1, &[0])
            .with_namespace_independent_members("api", &["make"])
            .build()]),
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

#[test]
fn dynamic_namespace_write_still_rejects_retention_and_is_inconclusive() {
    let rejected = run_rejection_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             let later = 0;\n\
             const callback = () => later;\n\
             const replacement = callback => ({ invoke: callback });\n\
             const method = 'make';\n\
             api[method] = replacement;\n\
             const wrapped = api.make(callback);\n\
             later = 42;\n",
            vec![
                logical_module_with_anon(
                    "callbacks/wrapped",
                    &[
                        Member::new("callback"),
                        Member::new("replacement"),
                        Member::new("method"),
                        Member::new("wrapped"),
                    ],
                    &["api[method] = replacement;"],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: callback }; } };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_retained_callback_factory_member("api", "make", 1, &[0])
            .with_namespace_independent_members("api", &["make"])
            .build()]),
    );

    let report = gate_diagnose_json(&rejected, "later", &[]);
    assert!(report["total_count"].as_u64().unwrap() > 0);
    assert_eq!(
        report["query_status"],
        "inconclusive_for_supported_guard_family"
    );
    assert_eq!(
        report["coverage_scope"],
        "callback_factory_namespace_member_write_guard_only"
    );
    assert_eq!(
        report["traces"][0]["coverage"],
        "inconclusive_unsupported_guard"
    );
    assert_eq!(report["traces"][0]["matched_contract"]["member"], "make");
    assert_eq!(
        report["traces"][0]["first_rejected_guard"]["code"],
        "unsupported_guard"
    );
    assert!(report["traces"][0]["first_rejected_guard"]["witness"].is_null());
}

#[test]
fn old_and_new_contracts_are_independent_and_unmatched_shapes_fall_back() {
    for (call, accepted) in [
        ("factory(() => later)", true),
        ("factory(callback, () => later)", true),
        ("factory(callback)", false),
    ] {
        let source = format!(
            "import {{ factory }} from './peer.js';\nconst callback = () => later;\nconst wrapped = {call};\nconst later = 42;\nconsole.log(wrapped.invoke());\n"
        );
        let modules = vec![
            logical_module(
                "callbacks/wrapped",
                &[Member::new("callback"), Member::new("wrapped")],
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
fn publishing_returned_graph_exposes_both_callback_positions() {
    let source = "import { factory } from './peer.js';\nimport { consume } from './host.js';\nlet later = 0;\nlet other = 0;\nconst callback = () => later;\nconst wrapped = factory(callback, () => other);\nconst observed = consume(wrapped);\nlater = 42;\nother = 7;\nconsole.log(observed, later, other);\n";
    let chunks = [
        ("static/peer", PROVIDER),
        (
            "static/host",
            "export function consume(value) { return Reflect.apply(value.compare, null, []); }\n",
        ),
    ];
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_retained_callback_factory("factory", 2, &[0, 1])
        .build()];
    let all: Vec<_> = ["callback", "wrapped", "observed", "later", "other"]
        .into_iter()
        .map(Member::new)
        .collect();
    let colocated = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "all",
                &all,
                &["later = 42;", "other = 7;"],
            )],
        )
        .with_extra_chunks(&chunks)
        .with_chunk_export_purity(&contracts),
    );
    assert_preserved_entry_output(&colocated, "0 42 7\n");
    assert_eager_read(&colocated, "observed", "later");
    assert_eager_read(&colocated, "observed", "other");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[
                        Member::new("callback"),
                        Member::new("wrapped"),
                        Member::new("observed"),
                    ],
                ),
                logical_module_with_anon(
                    "values/later",
                    &[Member::new("later"), Member::new("other")],
                    &["later = 42;", "other = 7;"],
                ),
            ],
        )
        .with_extra_chunks(&chunks)
        .with_chunk_export_purity(&contracts),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn callback_argument_evaluation_effects_remain_eager_and_ordered() {
    let source = "import { factory } from './peer.js';\nlet later = 0;\nconst callback = () => later;\nconst wrapped = factory((globalThis.seen = callback(), callback), () => later);\nlater = 42;\nconsole.log(globalThis.seen, later);\n";
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_retained_callback_factory("factory", 2, &[0, 1])
        .build()];
    let colocated = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "all",
                &[
                    Member::new("callback"),
                    Member::new("wrapped"),
                    Member::new("later"),
                ],
                &["later = 42;"],
            )],
        )
        .with_extra_chunks(&[("static/peer", PROVIDER)])
        .with_chunk_export_purity(&contracts),
    );
    assert_preserved_entry_output(&colocated, "0 42\n");
    assert_eager_read(&colocated, "wrapped", "later");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("callback"), Member::new("wrapped")],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&[("static/peer", PROVIDER)])
        .with_chunk_export_purity(&contracts),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn admitted_call_argument_retains_its_own_synchronous_effects() {
    let source = "import { factory } from './peer.js';\nlet later = 0;\nconst callback = () => later;\nfunction makeReader() { globalThis.seen = callback(); return () => later; }\nconst wrapped = factory(makeReader(), () => other);\nconst other = 7;\nlater = 42;\nconsole.log(globalThis.seen, wrapped.invoke(), wrapped.compare());\n";
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_retained_callback_factory("factory", 2, &[0, 1])
        .build()];
    let all: Vec<_> = ["callback", "makeReader", "wrapped", "later", "other"]
        .into_iter()
        .map(Member::new)
        .collect();
    let colocated = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon("all", &all, &["later = 42;"])],
        )
        .with_extra_chunks(&[("static/peer", PROVIDER)])
        .with_chunk_export_purity(&contracts),
    );
    assert_preserved_entry_output(&colocated, "0 42 7\n");
    assert_eager_read(&colocated, "wrapped", "later");
    let graph = colocated.dependency_graph();
    let wrapped = source_unit_for_binding(&graph, "wrapped");
    let other = source_unit_for_binding(&graph, "other");
    assert!(!graph.edges.iter().any(|edge| edge.source == wrapped
        && edge.target == other
        && edge.edge_kind == DepKind::EagerUse
        && edge.constrains_init_order));
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[
                        Member::new("callback"),
                        Member::new("makeReader"),
                        Member::new("wrapped"),
                    ],
                ),
                logical_module_with_anon(
                    "values/later",
                    &[Member::new("later"), Member::new("other")],
                    &["later = 42;"],
                ),
            ],
        )
        .with_extra_chunks(&[("static/peer", PROVIDER)])
        .with_chunk_export_purity(&contracts),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn previously_published_callback_keeps_conservative_timing() {
    expect_atomic_conflict_rejection(FixtureOpts::new(
        "import { factory } from './peer.js';\nconst callback = () => later;\nglobalThis.saved = callback;\nconst wrapped = factory(callback);\nconst later = 42;\nconsole.log(wrapped.invoke());\n",
        vec![logical_module_with_anon("callbacks/wrapped", &[Member::new("callback"), Member::new("wrapped")], &["globalThis.saved = callback;"]),
             logical_module("values/later", &[Member::new("later")])],
    ).with_extra_chunks(&[("static/peer", PROVIDER)]).with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
        .with_retained_callback_factory("factory", 1, &[0]).build()]), &["callbacks/wrapped", "values/later"], &[]);
}

#[test]
fn distinct_local_closure_instances_preserve_their_captured_callbacks() {
    let fixture = run_fixture(FixtureOpts::new(
        "import { factory } from './peer.js';\nfunction reader(callback) { return () => callback(); }\nconst first = reader(() => later);\nconst second = reader(() => other);\nconst wrapped = factory(first, second);\nconst later = 42;\nconst other = 7;\nconsole.log(wrapped.invoke(), wrapped.compare());\n",
        vec![logical_module("callbacks/wrapped", &[Member::new("reader"), Member::new("first"), Member::new("second"), Member::new("wrapped")]),
             logical_module("values/later", &[Member::new("later")]), logical_module("values/other", &[Member::new("other")])],
    ).with_extra_chunks(&[("static/peer", PROVIDER)]).with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
        .with_retained_callback_factory("factory", 2, &[0, 1]).build()]));
    assert_preserved_entry_output(&fixture, "42 7\n");
}
