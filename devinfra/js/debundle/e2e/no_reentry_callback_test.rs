//! Audited callback calls can register callbacks without invoking them yet.

use debundle_e2e_support::*;

const PROVIDER: &str = "const callbacks = new Set;\nexport function register(callback) { callbacks.add(callback); }\nexport function emit() { return [...callbacks].map(callback => callback()); }\n";

#[test]
fn registration_defers_callback_reads_without_asserting_purity() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { register, emit } from './peer.js';\nconst registration = register(() => later);\nconst later = 42;\nconsole.log(emit()[0]);\n",
            vec![
                logical_module("callbacks/registration", &[Member::new("registration")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", PROVIDER)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_no_reentry_callback("register", 1, &[0])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn stable_named_aliases_and_multiple_callbacks_accept_primitive_metadata() {
    let fixture = run_fixture(FixtureOpts::new(
        "import { register, emit } from './peer.js';\nfunction callback() { return later; }\nconst alias = callback;\nconst registration = register(alias, () => other, 'metadata');\nconst later = 42;\nconst other = 7;\nconsole.log(emit().join(' '));\n",
        vec![logical_module("callbacks/registration", &[Member::new("callback"), Member::new("alias"), Member::new("registration")]), logical_module("values/later", &[Member::new("later"), Member::new("other")])],
    ).with_extra_chunks(&[("static/peer", "const callbacks = new Set;\nexport function register(callback, other, metadata) { callbacks.add(callback); callbacks.add(other); }\nexport function emit() { return [...callbacks].map(callback => callback()); }\n")])
      .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer").with_no_reentry_callback("register", 3, &[0, 1]).build()]));
    assert_preserved_entry_output(&fixture, "42 7\n");
}

#[test]
fn later_opaque_registry_emission_retains_eager_reads_and_node_tdz_behavior() {
    let source = "import { register, emit } from './peer.js';\nconst registration = register(() => later);\nlet observed;\ntry { observed = emit()[0]; } catch (error) { observed = error.name; }\nconst later = 42;\nconsole.log(observed);\n";
    let policy = ChunkExportPurityBuilder::new("static/peer")
        .with_no_reentry_callback("register", 1, &[0])
        .build();
    let statements = &["try { observed = emit()[0]; } catch (error) { observed = error.name; }"];
    let colocated = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "all",
                &[
                    Member::new("registration"),
                    Member::new("observed"),
                    Member::new("later"),
                ],
                statements,
            )],
        )
        .with_extra_chunks(&[("static/peer", PROVIDER)])
        .with_chunk_export_purity(std::slice::from_ref(&policy)),
    );
    assert_preserved_entry_output(&colocated, "ReferenceError\n");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "callbacks/registration",
                    &[Member::new("registration"), Member::new("observed")],
                    statements,
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", PROVIDER)])
        .with_chunk_export_purity(&[policy]),
        &["callbacks/registration", "values/later"],
        &[],
    );
}

#[test]
fn sibling_and_outer_calls_observe_callbacks_registered_during_operand_evaluation() {
    for expression in [
        "(register(() => later), emit())",
        "consume(register(() => later))",
    ] {
        let source = format!(
            "import {{ register, emit, consume }} from './peer.js';\nlet later = 0;\nconst observed = {expression};\nlater = 42;\nconsole.log(observed[0], later);\n"
        );
        let provider = format!("{PROVIDER}export function consume(value) {{ return emit(); }}\n");
        let policy = ChunkExportPurityBuilder::new("static/peer")
            .with_no_reentry_callback("register", 1, &[0])
            .build();
        let colocated = run_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module_with_anon(
                    "all",
                    &[Member::new("later"), Member::new("observed")],
                    &["later = 42;"],
                )],
            )
            .with_extra_chunks(&[("static/peer", &provider)])
            .with_chunk_export_purity(std::slice::from_ref(&policy)),
        );
        assert_preserved_entry_output(&colocated, "0 42\n");
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module("callbacks/registration", &[Member::new("observed")]),
                    logical_module_with_anon(
                        "values/later",
                        &[Member::new("later")],
                        &["later = 42;"],
                    ),
                ],
            )
            .with_extra_chunks(&[("static/peer", &provider)])
            .with_chunk_export_purity(&[policy]),
            &["callbacks/registration", "values/later"],
            &[],
        );
    }
}

#[test]
fn protocol_operations_and_opaque_results_retain_registered_callbacks() {
    for (expression, provider) in [
        ("'probe' in proxy", format!("{PROVIDER}export const proxy = new Proxy({{}}, {{ has() {{ return emit()[0]; }} }});\n")),
        ("registration()", "const callbacks = new Set;\nexport function register(callback) { callbacks.add(callback); return () => [...callbacks][0](); }\n".to_string()),
    ] {
        let source = format!("import {{ register{} }} from './peer.js';\nlet later = 0;\nconst registration = register(() => later);\nconst observed = {expression};\nlater = 42;\nconsole.log(observed, later);\n", if expression.contains("proxy") { ", proxy" } else { "" });
        let policy = ChunkExportPurityBuilder::new("static/peer").with_no_reentry_callback("register", 1, &[0]).build();
        let colocated = run_fixture(FixtureOpts::new(&source, vec![logical_module_with_anon("all", &[Member::new("later"), Member::new("registration"), Member::new("observed")], &["later = 42;"])])
            .with_extra_chunks(&[("static/peer", &provider)]).with_chunk_export_purity(std::slice::from_ref(&policy)));
        assert_preserved_entry_output(&colocated, if expression.contains("proxy") { "false 42\n" } else { "0 42\n" });
        expect_atomic_conflict_rejection(FixtureOpts::new(&source, vec![
            logical_module("callbacks/registration", &[Member::new("registration"), Member::new("observed")]),
            logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
        ]).with_extra_chunks(&[("static/peer", &provider)]).with_chunk_export_purity(&[policy]), &["callbacks/registration", "values/later"], &[]);
    }
}

#[test]
fn unsupported_shapes_and_callback_surfaces_keep_conservative_timing() {
    let mut unexpected = Vec::new();
    for (declarations, call, names, statements) in [
        ("", "register(...[() => later])", vec![], vec![]),
        ("", "new register(() => later)", vec![], vec![]),
        ("", "register?.(() => later)", vec![], vec![]),
        ("", "({ register }).register(() => later)", vec![], vec![]),
        ("", "register(() => later, void 0, false)", vec![], vec![]),
        ("", "register(() => later, {})", vec![], vec![]),
        (
            "const alias = register;\n",
            "alias(() => later)",
            vec!["alias"],
            vec![],
        ),
        (
            "const object = { callback: () => later };\n",
            "register(object.callback)",
            vec!["object"],
            vec![],
        ),
        (
            "let callback = () => later;\ncallback = () => later;\n",
            "register(callback)",
            vec!["callback"],
            vec!["callback = () => later;"],
        ),
        (
            "function callback() { return 1; }\ncallback.extra = () => later;\n",
            "register(callback)",
            vec!["callback"],
            vec!["callback.extra = () => later;"],
        ),
        (
            "function callback() { return 1; }\nconst alias = callback;\nalias.prototype.extra = () => later;\n",
            "register(callback)",
            vec!["callback", "alias"],
            vec!["alias.prototype.extra = () => later;"],
        ),
        (
            "const callback = () => later;\npublish(callback);\n",
            "register(callback)",
            vec!["callback"],
            vec!["publish(callback);"],
        ),
        (
            "const callback = () => later;\nexport { callback };\n",
            "register(callback)",
            vec!["callback"],
            vec![],
        ),
        (
            "const callback = () => later;\nconst alias = callback;\nexport { alias };\n",
            "register(callback)",
            vec!["callback", "alias"],
            vec![],
        ),
    ] {
        let source = format!(
            "import {{ register, emit, publish }} from './peer.js';\n{declarations}const registration = {call};\nconst later = 42;\nconsole.log(emit()[0]);\n"
        );
        let mut members: Vec<_> = names.into_iter().map(Member::new).collect();
        members.push(Member::new("registration"));
        let provider =
            format!("{PROVIDER}export function publish(value) {{ globalThis.saved = value; }}\n");
        let outcome = std::panic::catch_unwind(|| {
            expect_atomic_conflict_rejection(
                FixtureOpts::new(
                    &source,
                    vec![
                        logical_module_with_anon("callbacks/registration", &members, &statements),
                        logical_module("values/later", &[Member::new("later")]),
                    ],
                )
                .with_extra_chunks(&[("static/peer", &provider)])
                .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
                    .with_no_reentry_callback("register", 1, &[0])
                    .with_no_reentry_callback("register", 2, &[0])
                    .build()]),
                &["callbacks/registration", "values/later"],
                &[],
            )
        });
        if outcome.is_err() {
            unexpected.push(format!("{call}; declarations: {declarations:?}"));
        }
    }
    assert!(unexpected.is_empty(), "unexpected cases: {unexpected:#?}");
}

#[test]
fn callback_argument_evaluation_and_signature_validation_remain_checked() {
    expect_atomic_conflict_rejection(FixtureOpts::new(
        "import { register } from './peer.js';\nconst registration = register(callback);\nconst callback = () => later;\nconst later = 42;\nconsole.log(later);\n",
        vec![logical_module("callbacks/registration", &[Member::new("registration")]), logical_module("values/later", &[Member::new("callback"), Member::new("later")])],
    ).with_extra_chunks(&[("static/peer", PROVIDER)]).with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer").with_no_reentry_callback("register", 1, &[0]).build()]),
    &["callbacks/registration", "values/later"], &[]);
    for (count, positions, message) in [
        (1, vec![], "must list at least one callback argument"),
        (1, vec![1], "position must be less than argument_count"),
    ] {
        expect_rejection_containing_all(
            FixtureOpts::new("console.log(0);\n", vec![])
                .with_extra_chunks(&[("static/peer", PROVIDER)])
                .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
                    .with_no_reentry_callback("register", count, &positions)
                    .build()]),
            &[message],
        );
    }
}
