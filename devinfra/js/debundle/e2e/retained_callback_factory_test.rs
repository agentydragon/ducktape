//! Separately audited factories retain existing and multiple callbacks.

use analysis::DepKind;
use debundle_e2e_support::*;

const PROVIDER: &str =
    "export function factory(callback, compare) { return { invoke: callback, compare }; }\n";

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
