//! An audited descriptor-copy provider retains input methods and getters only
//! through its returned ordinary wrapper.

use debundle_e2e_support::*;

const PROVIDER: &str = "export function wrap(definition) { const create = definition.create; const extra = { create: (...args) => create.call(definition, ...args), call: () => create.call(definition) }; return Object.assign(Object.defineProperties({ enabled: () => true, userFacingName: () => definition.name }, Object.getOwnPropertyDescriptors(definition)), extra); }";

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

#[test]
fn copied_getter_and_method_remain_deferred_until_returned_wrapper_use() {
    let source = "import { wrap } from './peer.js';\nconst wrapped = wrap({ name: 'report', create() { return later(); }, get schema() { return later(); } });\nvar later = () => 42;\nconsole.log(wrapped.schema, wrapped.create());\n";
    let chunks = [("static/peer", PROVIDER)];
    let contracts = [contract()];
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
fn copied_getter_read_before_later_initialization_is_eager() {
    let source = "import { wrap } from './peer.js';\nconst wrapped = wrap({ create() { return later(); }, get schema() { return later(); } });\nconst observed = wrapped.schema;\nvar later = () => 42;\n";
    let chunks = [("static/peer", PROVIDER)];
    let contracts = [contract()];
    expect_atomic_conflict_rejection(
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
        &["wrapped", "later"],
        &[],
    );
}

#[test]
fn eager_create_getter_and_assigned_setter_collision_are_not_admitted() {
    let chunks = [("static/peer", PROVIDER)];
    for field in [
        "get create() { return () => later(); }",
        "create() { return 1; }, set call(value) { later(); }",
    ] {
        let source = format!(
            "import {{ wrap }} from './peer.js';\nconst wrapped = wrap({{ {field} }});\nvar later = () => 42;\n"
        );
        let contracts = [contract()];
        expect_atomic_conflict_rejection(
            FixtureOpts::new(&source, modules())
                .with_extra_chunks(&chunks)
                .with_chunk_export_purity(&contracts),
            &["wrapped", "later"],
            &[],
        );
    }
}

#[test]
fn known_descriptor_native_mutation_rejects_author_contract() {
    let source = "import { wrap } from './peer.js';\nObject.getOwnPropertyDescriptors = record => record.create();\nconst wrapped = wrap({ create() { return later(); } });\nvar later = () => 42;\n";
    let chunks = [("static/peer", PROVIDER)];
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
    let provider = format!("{PROVIDER}\nwrap = value => value;");
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
        PROVIDER.replacen(
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
    let chunks = [("static/peer", PROVIDER)];
    let contracts = [contract()];
    expect_atomic_conflict_rejection(
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
        &["wrapped", "later"],
        &[],
    );
}

#[test]
fn unlisted_result_property_remains_conservative() {
    let source = "import { wrap } from './peer.js';\nconst wrapped = wrap({ create() { return later(); } });\nconst unknown = wrapped.unlisted;\nvar later = () => 42;\n";
    let chunks = [("static/peer", PROVIDER)];
    let contracts = [contract()];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("wrapped", &[Member::new("wrapped"), Member::new("unknown")]),
                logical_module("later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&chunks)
        .with_chunk_export_purity(&contracts),
        &["wrapped", "later"],
        &[],
    );
}

#[test]
fn known_prototype_setter_mutation_rejects_author_contract() {
    let source = "import { wrap } from './peer.js';\nObject.defineProperty(Object.prototype, 'call', { set(value) { later(); } });\nconst wrapped = wrap({ create() { return 1; } });\nvar later = () => 42;\n";
    let chunks = [("static/peer", PROVIDER)];
    let contracts = [contract()];
    expect_rejection_containing_all(
        FixtureOpts::new(source, modules())
            .with_extra_chunks(&chunks)
            .with_chunk_export_purity(&contracts),
        &["descriptor_copy_factory_exports", "Object slot"],
    );
}
