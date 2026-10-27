//! Constructor callback retention is independent of call timing and purity.

use debundle_e2e_support::*;

const PROVIDER: &str = "export class Box { constructor(callback, compare, metadata) { this.callback = callback; this.compare = compare; this.metadata = metadata; } invoke() { return this.callback(); } }\n";

fn contract() -> ChunkExportPurityBuilder {
    ChunkExportPurityBuilder::new("static/peer")
        .with_pure_new_exports(&["Box"])
        .with_retained_callback_constructor("Box", 1, &[0])
        .with_retained_callback_constructor("Box", 3, &[0, 1])
}

#[test]
fn audited_constructor_retains_callback_until_instance_use() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { Box } from './peer.js';\nconst wrapped = new Box(() => later);\nconst later = 42;\nconsole.log(wrapped.invoke());\n",
            vec![
                logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export class Box { constructor(callback) { this.callback = callback; } invoke() { return this.callback(); } }\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_pure_new_exports(&["Box"])
            .with_retained_callback_constructor("Box", 1, &[0])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn named_callbacks_aliases_and_primitive_metadata_remain_lazy() {
    let fixture = run_fixture(FixtureOpts::new(
        "import { Box as LocalBox } from './peer.js';\nfunction callback() { return later; }\nconst alias = callback;\nconst wrapped = new LocalBox(alias, () => other, 'metadata');\nconst later = 42;\nconst other = 7;\nconsole.log(wrapped.invoke(), wrapped.compare(), wrapped.metadata);\n",
        vec![logical_module("callbacks/wrapped", &[Member::new("callback"), Member::new("alias"), Member::new("wrapped")]), logical_module("values/later", &[Member::new("later"), Member::new("other")])],
    ).with_extra_chunks(&[("static/peer", PROVIDER)]).with_chunk_export_purity(&[contract().build()]));
    assert_preserved_entry_output(&fixture, "42 7 metadata\n");
}

#[test]
fn default_import_uses_the_defining_export_contract() {
    let fixture = run_fixture(FixtureOpts::new(
        "import LocalBox from './peer.js';\nconst wrapped = new LocalBox(() => later);\nconst later = 42;\nconsole.log(wrapped.callback());\n",
        vec![logical_module("callbacks/wrapped", &[Member::new("wrapped")]), logical_module("values/later", &[Member::new("later")])],
    ).with_extra_chunks(&[("static/peer", "export default class Box { constructor(callback) { this.callback = callback; } }\n")])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer").with_pure_new_exports(&["default"]).with_retained_callback_constructor("default", 1, &[0]).build()]));
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn a_retained_instance_can_flow_into_another_audited_constructor() {
    let fixture = run_fixture(FixtureOpts::new(
        "import { Box } from './peer.js';\nconst inner = new Box(() => later);\nconst wrapped = new Box(inner, () => later, 0);\nconst later = 42;\nconsole.log(wrapped.callback.callback(), wrapped.compare());\n",
        vec![logical_module("callbacks/wrapped", &[Member::new("inner"), Member::new("wrapped")]), logical_module("values/later", &[Member::new("later")])],
    ).with_extra_chunks(&[("static/peer", PROVIDER)]).with_chunk_export_purity(&[contract().build()]));
    assert_preserved_entry_output(&fixture, "42 42\n");
}

#[test]
fn invalid_constructor_signatures_report_spec_errors() {
    for (count, positions, message) in [
        (1, vec![], "must list at least one callback argument"),
        (1, vec![1], "position must be less than argument_count"),
    ] {
        expect_rejection_containing_all(
            FixtureOpts::new("console.log(0);\n", vec![])
                .with_extra_chunks(&[("static/peer", PROVIDER)])
                .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
                    .with_retained_callback_constructor("Box", count, &positions)
                    .build()]),
            &[message],
        );
    }
}

#[test]
fn unsupported_arguments_aliases_and_callback_mutations_keep_conservative_timing() {
    for (declarations, allocation, names, statements) in [
        ("", "new Box(() => later, void 0)", vec![], vec![]),
        ("", "new Box(...[() => later])", vec![], vec![]),
        (
            "const Alias = Box;\n",
            "new Alias(() => later)",
            vec!["Alias"],
            vec![],
        ),
        (
            "let callback = () => later;\ncallback = () => later;\n",
            "new Box(callback)",
            vec!["callback"],
            vec!["callback = () => later;"],
        ),
        (
            "const callback = () => later;\nconst object = { callback };\n",
            "new Box(object.callback)",
            vec!["callback", "object"],
            vec![],
        ),
        ("", "new Box(() => later, () => later, {})", vec![], vec![]),
        (
            "function callback() { return 1; }\ncallback.extra = () => later;\n",
            "new Box(callback)",
            vec!["callback"],
            vec!["callback.extra = () => later;"],
        ),
    ] {
        let source = format!(
            "import {{ Box }} from './peer.js';\n{declarations}const wrapped = {allocation};\nconst later = 42;\nconsole.log(wrapped.invoke());\n"
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
            .with_chunk_export_purity(&[contract().build()]),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
    }
}

#[test]
fn ordinary_call_contract_and_constructor_purity_do_not_defer_construction() {
    let source = "import { Box } from './peer.js';\nconst wrapped = new Box(() => later);\nconst later = 42;\nconsole.log(wrapped.invoke());\n";
    for policy in [
        ChunkExportPurityBuilder::new("static/peer")
            .with_pure_new_exports(&["Box"])
            .build(),
        ChunkExportPurityBuilder::new("static/peer")
            .with_pure_new_exports(&["Box"])
            .with_retained_callback_factory("Box", 1, &[0])
            .build(),
    ] {
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                source,
                vec![
                    logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                    logical_module("values/later", &[Member::new("later")]),
                ],
            )
            .with_extra_chunks(&[("static/peer", PROVIDER)])
            .with_chunk_export_purity(&[policy]),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
    }
}

#[test]
fn constructor_contract_does_not_defer_an_ordinary_call() {
    expect_atomic_conflict_rejection(FixtureOpts::new(
        "import { Box } from './peer.js';\nconst wrapped = Box(() => later);\nconst later = 42;\nconsole.log(wrapped.invoke());\n",
        vec![logical_module("callbacks/wrapped", &[Member::new("wrapped")]), logical_module("values/later", &[Member::new("later")])],
    ).with_extra_chunks(&[("static/peer", "export function Box(callback) { return { invoke: callback }; }\n")]).with_chunk_export_purity(&[contract().build()]), &["callbacks/wrapped", "values/later"], &[]);
}

#[test]
fn retention_does_not_assert_constructor_purity_or_remove_effect_order() {
    let fixture = run_fixture(FixtureOpts::new(
        "import { Box } from './peer.js';\nconst before = console.log('before');\nconst wrapped = new Box(() => later);\nconst later = 42;\nconsole.log('after', wrapped.callback());\n",
        vec![logical_module("all", &[Member::new("before"), Member::new("wrapped"), Member::new("later")])],
    ).with_extra_chunks(&[("static/peer", "export class Box { constructor(callback) { console.log('construct'); this.callback = callback; } }\n")])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer").with_retained_callback_constructor("Box", 1, &[0]).build()]));
    assert_preserved_entry_output(&fixture, "before\nconstruct\nafter 42\n");
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
    let purity = serde_json::to_value(&wrapped.purity).unwrap();
    assert_eq!(purity["kind"], "not_pure");
    assert!(
        purity["reasons"]
            .as_array()
            .unwrap()
            .iter()
            .any(|reason| reason["rule"] == "unknown_new")
    );
}

#[test]
fn evaluated_callback_argument_still_has_its_tdz_dependency() {
    expect_atomic_conflict_rejection(FixtureOpts::new(
        "import { Box } from './peer.js';\nconst wrapped = new Box(callback);\nconst callback = () => later;\nconst later = 42;\nconsole.log(wrapped.callback());\n",
        vec![logical_module("callbacks/wrapped", &[Member::new("wrapped")]), logical_module("values/later", &[Member::new("callback"), Member::new("later")])],
    ).with_extra_chunks(&[("static/peer", PROVIDER)]).with_chunk_export_purity(&[contract().build()]), &["callbacks/wrapped", "values/later"], &[]);
}

#[test]
fn extraction_invocation_and_opaque_publication_promote_all_retained_callbacks() {
    for (projection, expression) in [
        ("callback", "extracted()"),
        ("compare", "extracted()"),
        ("callback", "consume(wrapped)"),
    ] {
        let source = format!(
            "import {{ Box, consume }} from './peer.js';\nlet later = 0;\nlet other = 0;\nconst wrapped = new Box(() => () => later, () => () => other, null);\nconst extracted = wrapped.{projection};\nconst observed = {expression};\nlater = 42;\nother = 7;\nconsole.log(typeof observed, later, other);\n"
        );
        let provider =
            format!("{PROVIDER}export function consume(value) {{ return value.callback()(); }}\n");
        let all: Vec<_> = ["later", "other", "wrapped", "extracted", "observed"]
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
            .with_extra_chunks(&[("static/peer", &provider)])
            .with_chunk_export_purity(&[contract().build()]),
        );
        assert_preserved_entry_output(
            &colocated,
            if expression == "consume(wrapped)" {
                "number 42 7\n"
            } else {
                "function 42 7\n"
            },
        );
        let graph = colocated.dependency_graph();
        let observed = source_unit_for_binding(&graph, "observed");
        for name in ["later", "other"] {
            let target = source_unit_for_binding(&graph, name);
            assert!(graph.edges.iter().any(|edge| edge.source == observed
                && edge.target == target
                && edge.edge_kind == analysis::DepKind::EagerUse
                && edge.constrains_init_order));
        }
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module(
                        "callbacks/wrapped",
                        &[
                            Member::new("wrapped"),
                            Member::new("extracted"),
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
            .with_extra_chunks(&[("static/peer", &provider)])
            .with_chunk_export_purity(&[contract().build()]),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
    }
}
