//! An audited descriptor-copy provider retains input methods and getters only
//! through its returned ordinary wrapper.

use debundle_e2e_graph_support::FixtureGraphExt;
use debundle_e2e_support::*;

fn modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module("wrapped", &[Member::new("wrapped")]),
        logical_module("later", &[Member::new("later")]),
    ]
}

fn contract() -> (&'static str, ChunkExportPurity) {
    ChunkExportPurityBuilder::new("static/peer")
        .with_descriptor_copy_factory("wrap", &["create"], &["create", "call"])
        .build()
}

fn assert_descriptor_module_placement(fixture: &Fixture, extra_binding: Option<&str>) {
    let extra_bindings = extra_binding.map(|binding| [binding]);
    let mut declarations = vec![(
        VariableDeclarationKind::Const,
        &["wrapped"][..],
        VariableInitializerKind::Call,
    )];
    if let Some(bindings) = extra_bindings.as_ref() {
        declarations.push((
            VariableDeclarationKind::Const,
            bindings.as_slice(),
            VariableInitializerKind::Other,
        ));
    }
    assert_module_variable_declarators(
        &fixture.out_root,
        "static/app/modules/wrapped.js",
        &declarations,
    );
    assert_module_variable_declarators(
        &fixture.out_root,
        "static/app/modules/later.js",
        &[(
            VariableDeclarationKind::Var,
            &["later"],
            VariableInitializerKind::Arrow,
        )],
    );
}

fn assert_descriptor_input_not_admitted(fixture: &Fixture) {
    let graph = fixture.dependency_graph();
    let wrapped = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "wrapped")
        })
        .unwrap();
    assert!(
        wrapped
            .synchronous_execution
            .as_ref()
            .unwrap()
            .inference_uses
            .iter()
            .all(|usage| usage.descriptor_copy_author_refs.is_empty()),
        "invalid input must not consume the descriptor-copy contract: {wrapped:#?}"
    );
}

#[test]
fn copied_getter_and_method_remain_deferred_until_returned_wrapper_use() {
    let source = "import { wrap } from './peer.js';\nconst wrapped = wrap({ name: 'report', create() { return later(); }, get schema() { return later(); } });\nvar later = () => 42;\nconsole.log(wrapped.schema, wrapped.create());\n";
    let chunks = [("static/peer", DESCRIPTOR_COPY_PROVIDER)];
    let contracts = [contract()];
    let baseline = run_fixture(
        FixtureOpts::new(source, vec![])
            .with_extra_chunks(&chunks)
            .with_chunk_export_purity(&contracts),
    );
    let report: serde_json::Value =
        read_json(&baseline.report_root.join("static/app/modules.json"));
    let summary = &report["imported_callables"]["wrap"];
    assert!(
        summary["conditional_bodies"]
            .as_array()
            .is_some_and(|bodies| !bodies.is_empty()),
        "conditional source body must be available before checking relocation: {summary:#}"
    );
    let fixture = run_fixture(
        FixtureOpts::new(source, modules())
            .with_extra_chunks(&chunks)
            .with_chunk_export_purity(&contracts),
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/wrapped.js",
        &["wrapped"],
        &[],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/later.js",
        &["later"],
        &[],
    );
    assert_preserved_entry_output(&fixture, "42 42\n");
    let graph = fixture.dependency_graph();
    let wrapped = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "wrapped")
        })
        .expect("wrapped source unit");
    let execution = serde_json::to_value(wrapped.synchronous_execution.as_ref().unwrap()).unwrap();
    assert_eq!(
        execution["binding_effect_authority"]["authority"], "operation_receipt",
        "{execution:#}"
    );
    assert_eq!(
        execution["binding_effect_authority"]["receipt"]["calls"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    assert!(
        execution["inference_uses"]
            .as_array()
            .unwrap()
            .iter()
            .any(|usage| usage["conclusion"] == "conditional_foreign_body"),
        "{execution:#}"
    );
    assert!(
        fixture.effect_translation_validation()["swapped_pairs"]
            .as_u64()
            .unwrap()
            > 0
    );
    assert!(
        !wrapped.purity.is_pure(),
        "the provider call and its possible effects remain impure"
    );
    assert!(wrapped.synchronous_execution.as_ref().expect("synchronous execution").inference_uses.iter().any(|usage| {
        usage.descriptor_copy_author_refs.iter().any(|author| {
            author.defining_module == "static/peer" && author.export == "wrap"
                && author.spec_location == "chunk_export_purity.static/peer.descriptor_copy_factory_exports.wrap"
        })
    }));
}

#[test]
fn descriptor_timing_does_not_cover_an_uninterpreted_provider_body() {
    let provider = format!(
        "function touch() {{ globalThis.marker = 1; }} {}",
        DESCRIPTOR_COPY_PROVIDER.replacen(
            "const create = definition.create;",
            "touch(); const create = definition.create;",
            1
        )
    );
    let chunks = [("static/peer", provider.as_str())];
    let contracts = [contract()];
    let fixture = run_fixture(FixtureOpts::new(
        "import { wrap } from './peer.js'; const wrapped = wrap({ create() { return later(); } }); var later = () => 42; console.log(globalThis.marker, wrapped.create());\n",
        vec![],
    ).with_extra_chunks(&chunks).with_chunk_export_purity(&contracts));
    assert_preserved_entry_output(&fixture, "1 42\n");
    let graph = fixture.dependency_graph();
    let node = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "wrapped")
        })
        .unwrap();
    let execution = serde_json::to_value(node.synchronous_execution.as_ref().unwrap()).unwrap();
    assert_ne!(
        execution["binding_effect_authority"]["authority"], "operation_receipt",
        "{execution:#}"
    );
    assert!(
        execution["inference_uses"]
            .as_array()
            .unwrap()
            .iter()
            .all(|usage| usage["conclusion"] != "conditional_foreign_body"),
        "{execution:#}"
    );
}

#[test]
fn descriptor_return_identity_does_not_remove_body_or_argument_throws() {
    let provider_throw = DESCRIPTOR_COPY_PROVIDER.replacen(
        "const create = definition.create;",
        "throw new Error('provider failure'); const create = definition.create;",
        1,
    );
    for (case, source, provider, error) in [
        (
            "provider",
            "import { wrap } from './peer.js'; const wrapped = wrap({ create() { return later(); } }); var later = () => 42;",
            provider_throw.as_str(),
            "Error: provider failure",
        ),
        (
            "argument",
            "import { wrap } from './peer.js'; const wrapped = wrap({ name: (() => { throw new Error('argument failure'); })(), create() { return later(); } }); var later = () => 42;",
            DESCRIPTOR_COPY_PROVIDER,
            "Error: argument failure",
        ),
    ] {
        let chunks = [("static/peer", provider)];
        let contracts = [contract()];
        let fixture = run_fixture(
            FixtureOpts::new(source, vec![])
                .with_extra_chunks(&chunks)
                .with_chunk_export_purity(&contracts),
        );
        assert_preserved_entry_error_with_host_setup(
            &fixture,
            "",
            error,
            &format!("descriptor-{case}-throw"),
        );
        let graph = fixture.dependency_graph();
        let node = graph
            .nodes
            .iter()
            .find(|node| {
                node.declared_bindings
                    .iter()
                    .any(|binding| binding.binding == "wrapped")
            })
            .unwrap();
        let execution = serde_json::to_value(node.synchronous_execution.as_ref().unwrap()).unwrap();
        assert_ne!(
            execution["binding_effect_authority"]["authority"], "operation_receipt",
            "{case}: {execution:#}"
        );
    }
}

#[test]
fn local_descriptor_assignment_wrapper_derives_effects_without_annotation() {
    let source = format!(
        "{DESCRIPTOR_COPY_PROVIDER}\nconst wrapped = wrap({{ name: 'report', create() {{ return later(); }}, get schema() {{ return later(); }} }});\nvar later = () => 42;\nconsole.log(wrapped.schema, wrapped.create(), wrapped.enabled(), wrapped.userFacingName());\n"
    );
    let fixture = run_fixture(FixtureOpts::new(
        &source,
        vec![
            logical_module("wrapped", &[Member::new("wrap"), Member::new("wrapped")]),
            logical_module("later", &[Member::new("later")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "42 42 true report\n");
    assert!(
        fixture.effect_translation_validation()["swapped_pairs"]
            .as_u64()
            .unwrap()
            > 0
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
        .unwrap();
    let execution = serde_json::to_value(wrapped.synchronous_execution.as_ref().unwrap()).unwrap();
    assert_eq!(execution["complete"], true, "{execution:#}");
    assert_eq!(
        execution["binding_effect_authority"]["authority"], "operation_receipt",
        "{execution:#}"
    );
}

#[test]
fn copied_getter_read_before_later_initialization_is_eager() {
    let source = "import { wrap } from './peer.js';\nconst wrapped = wrap({ create() { return later(); }, get schema() { return later(); } });\nconst observed = wrapped.schema;\nvar later = () => 42;\n";
    let chunks = [("static/peer", DESCRIPTOR_COPY_PROVIDER)];
    let contracts = [contract()];
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module(
                    "wrapped",
                    &[Member::new("wrapped"), Member::new("observed")],
                ),
                logical_module("later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&chunks)
        .with_chunk_export_purity(&contracts),
    );
    assert_preserved_entry_error_with_host_setup(
        &fixture,
        "",
        "TypeError",
        "descriptor-copied-getter-before-initialization",
    );
    assert_descriptor_module_placement(&fixture, Some("observed"));
}

#[test]
fn eager_create_getter_and_assigned_setter_collision_are_not_admitted() {
    let chunks = [("static/peer", DESCRIPTOR_COPY_PROVIDER)];
    for (case, field, error) in [
        (
            "eager-getter",
            "get create() { return () => later(); }",
            "TypeError: Cannot set property create",
        ),
        (
            "setter-collision",
            "create() { return 1; }, set call(value) { later(); }",
            "TypeError",
        ),
    ] {
        let source = format!(
            "import {{ wrap }} from './peer.js';\nconst wrapped = wrap({{ {field} }});\nvar later = () => 42;\n"
        );
        let contracts = [contract()];
        let fixture = run_fixture(
            FixtureOpts::new(&source, modules())
                .with_extra_chunks(&chunks)
                .with_chunk_export_purity(&contracts),
        );
        assert_descriptor_input_not_admitted(&fixture);
        let case = format!("descriptor-invalid-input-{case}");
        assert_preserved_entry_error_with_host_setup(&fixture, "", error, &case);
        assert_descriptor_module_placement(&fixture, None);
    }
}

#[test]
fn known_descriptor_native_mutation_rejects_author_contract() {
    let source = "import { wrap } from './peer.js';\nObject.getOwnPropertyDescriptors = record => record.create();\nconst wrapped = wrap({ create() { return later(); } });\nvar later = () => 42;\n";
    let chunks = [("static/peer", DESCRIPTOR_COPY_PROVIDER)];
    let contracts = [contract()];
    expect_rejection_containing_all(
        FixtureOpts::new(source, modules())
            .with_extra_chunks(&chunks)
            .with_chunk_export_purity(&contracts),
        &["descriptor_copy_factory_exports", "Object slot"],
    );
}

#[test]
fn provider_export_rebinding_rejects_author_contract() {
    let source = "import { wrap } from './peer.js';\nconst wrapped = wrap({ create() { return later(); } });\nvar later = () => 42;\n";
    let provider = format!("{DESCRIPTOR_COPY_PROVIDER}\nwrap = value => value;");
    let chunks = [("static/peer", provider.as_str())];
    let contracts = [contract()];
    expect_rejection_containing_all(
        FixtureOpts::new(source, modules())
            .with_extra_chunks(&chunks)
            .with_chunk_export_purity(&contracts),
        &["descriptor_copy_factory_exports", "stable, unrebound"],
    );
}

#[test]
fn argument_effects_run_before_registration_and_getter_runs_at_read() {
    let source = "import { wrap, registrationCount } from './peer.js';\nconst events = [];\nconst wrapped = wrap({ name: (events.push('argument'), 'report'), create() { return later(); }, get schema() { events.push('getter'); return later(); } });\nevents.push('registered');\nvar later = () => 42;\nconsole.log(events.join('; '), wrapped.schema, events.join('; '), registrationCount());\n";
    let provider = format!(
        "let registrations = 0; export const registrationCount = () => registrations; {}",
        DESCRIPTOR_COPY_PROVIDER.replacen(
            "const create = definition.create;",
            "registrations++; const create = definition.create;",
            1,
        ),
    );
    let chunks = [("static/peer", provider.as_str())];
    let contracts = [contract()];
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "wrapped",
                    &[Member::new("events"), Member::new("wrapped")],
                    &["events.push('registered');"],
                ),
                logical_module("later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&chunks)
        .with_chunk_export_purity(&contracts),
    );
    assert_preserved_entry_output(
        &fixture,
        "argument; registered 42 argument; registered; getter 1\n",
    );
}

#[test]
fn opaque_use_of_result_before_later_initialization_promotes_captures() {
    let source = "import { wrap } from './peer.js';\nconst wrapped = wrap({ create() { return later(); }, get schema() { return later(); } });\nconsume(wrapped);\nvar later = () => 42;\n";
    let chunks = [("static/peer", DESCRIPTOR_COPY_PROVIDER)];
    let contracts = [contract()];
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "wrapped",
                    &[Member::new("wrapped")],
                    &["consume(wrapped);"],
                ),
                logical_module("later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&chunks)
        .with_chunk_export_purity(&contracts),
    );
    for (case, access) in [("getter", "record.schema"), ("method", "record.create()")] {
        let host = format!(
            "globalThis.consume = record => {{ try {{ console.log({access}); }} catch (error) {{ console.log(error.name); }} }};"
        );
        assert_preserved_entry_output_with_host_setup(
            &fixture,
            &host,
            "TypeError\n",
            &format!("descriptor-opaque-use-{case}"),
        );
    }
    assert_descriptor_module_placement(&fixture, None);
    let report = fixture.effect_translation_validation();
    assert!(
        report["coverage"]["unknown_coverage"]["host_effects"]
            .as_u64()
            .unwrap()
            > 0,
        "{report}"
    );
}

#[test]
fn unlisted_result_property_remains_conservative() {
    let source = "import { wrap } from './peer.js';\nconst wrapped = wrap({ create() { return later(); } });\nconst unknown = wrapped.unlisted;\nvar later = () => 42;\n";
    let chunks = [("static/peer", DESCRIPTOR_COPY_PROVIDER)];
    let contracts = [contract()];
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module("wrapped", &[Member::new("wrapped"), Member::new("unknown")]),
                logical_module("later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&chunks)
        .with_chunk_export_purity(&contracts),
    );
    assert_preserved_entry_output_with_host_setup(
        &fixture,
        "",
        "",
        "descriptor-unlisted-result-property",
    );
    assert_descriptor_module_placement(&fixture, Some("unknown"));
    let graph = fixture.dependency_graph();
    let unknown = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "unknown")
        })
        .unwrap();
    let authority = serde_json::to_value(
        &unknown
            .synchronous_execution
            .as_ref()
            .unwrap()
            .binding_effect_authority,
    )
    .unwrap();
    assert_ne!(authority["authority"], "operation_receipt", "{authority}");
    let report = fixture.effect_translation_validation();
    assert!(
        report["coverage"]["unknown_coverage"]["general_heap"]
            .as_u64()
            .unwrap()
            > 0,
        "{report}"
    );
}

#[test]
fn known_prototype_setter_mutation_rejects_author_contract() {
    let source = "import { wrap } from './peer.js';\nObject.defineProperty(Object.prototype, 'call', { set(value) { later(); } });\nconst wrapped = wrap({ create() { return 1; } });\nvar later = () => 42;\n";
    let chunks = [("static/peer", DESCRIPTOR_COPY_PROVIDER)];
    let contracts = [contract()];
    expect_rejection_containing_all(
        FixtureOpts::new(source, modules())
            .with_extra_chunks(&chunks)
            .with_chunk_export_purity(&contracts),
        &["descriptor_copy_factory_exports", "Object slot"],
    );
}
