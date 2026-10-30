//! A checked implicit constructor can be reordered without executing its
//! deferred methods or field callbacks during module initialization.

use analysis::Purity;
use analysis::analysis_inputs::{ProofDependencies, ValueInputRef};
use analysis::reports::schema::{
    DependencyGraphReport, InferenceRule, InferenceUse, NativeIntegrityGuard, NativeIntegrityUse,
    SourceUnitReport,
};
use debundle_e2e_support::*;
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;

const REGISTRY_SOURCE: &str = r#"globalThis.events = [];
const a = (() => 1)();
class Registry {
  #items = new Set();
  #active = !1;
  onExit = () => this.clear();
  get size() { return this.#items.size; }
  clear() { globalThis.events.push("clear"); }
}
const b = new Registry;
const c = b.size + a;
b.onExit();
console.log(c, globalThis.events.join(","));
export { a, b, c, Registry };
"#;

fn registry_modules() -> Vec<LogicalModuleEntry> {
    vec![logical_module(
        "registry",
        &[Member::new("Registry"), Member::new("b")],
    )]
}

#[test]
fn failed_constructor_value_proof_keeps_record_callbacks_across_cycle() {
    let provider = r#"import { enter } from "./consumer.js";
class Holder {
  constructor(record) { this.record = record; this.missing = record.missing; }
  run() { return this.record.execute(); }
}
function create(record) { return new Holder(record); }
const prefix = new globalThis.Error();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 42;
})({});
const command = create({ execute: () => later });
enter();
const later = dynamic;
export { command };
"#;
    let consumer = r#"import { command } from "./provider.js";
export function enter() {
  try { console.log(command.run()); }
  catch (error) { console.log(error.name); }
}
"#;
    let consumer_chunks = [("static/consumer", consumer)];

    let co_located = run_fixture(
        FixtureOpts::new(
            provider,
            vec![logical_module_with_anon(
                "callbacks/wrapped",
                &[
                    Member::new("Holder"),
                    Member::new("create"),
                    Member::new("prefix"),
                    Member::new("dynamic"),
                    Member::new("command"),
                    Member::new("later"),
                ],
                &["enter();"],
            )],
        )
        .with_chunk_id("static/provider")
        .with_extra_chunks(&consumer_chunks),
    );
    assert_preserved_entry_output(&co_located, "ReferenceError\n");

    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            provider,
            vec![
                logical_module_with_anon(
                    "callbacks/wrapped",
                    &[
                        Member::new("Holder"),
                        Member::new("create"),
                        Member::new("command"),
                    ],
                    &["enter();"],
                ),
                logical_module(
                    "values/dynamic",
                    &[Member::new("prefix"), Member::new("dynamic")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_chunk_id("static/provider")
        .with_extra_chunks(&consumer_chunks),
        &["callbacks/wrapped", "values/later"],
        &["eager_use"],
    );
}

#[test]
fn inferred_implicit_class_construction_breaks_a_live_array_sequenced_cycle() {
    let fixture = assert_pure_cycle_break(
        REGISTRY_SOURCE,
        registry_modules(),
        "registry",
        &["class Registry", "const b = new Registry"],
        &["const a"],
        "1 clear\n",
    );
    assert_preserved_entry_output(&fixture, "1 clear\n");
}

#[test]
fn inferred_implicit_class_construction_breaks_an_array_free_sequenced_cycle() {
    let source = REGISTRY_SOURCE
        .replace("globalThis.events = [];", "globalThis.events = '';")
        .replace(
            "globalThis.events.push(\"clear\");",
            "globalThis.events += 'clear';",
        )
        .replace("globalThis.events.join(\",\")", "globalThis.events");
    let fixture = assert_pure_cycle_break(
        &source,
        registry_modules(),
        "registry",
        &["class Registry", "const b = new Registry"],
        &["const a"],
        "1 clear\n",
    );
    assert_preserved_entry_output(&fixture, "1 clear\n");
}

#[test]
fn void_zero_field_does_not_fabricate_callback_reentry() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const saved = () => later;
globalThis.saved = saved;
class Checker {
  result = void 0;
  isAllowed() { return true; }
}
globalThis.savedChecker = Checker;
class Command {
  constructor(config, checker) { this.config = config; this.checker = checker; }
  execute() { return this.config.execute(); }
}
function create(config) { return new Command(config, new Checker()); }
const command = create({ execute: () => later });
const later = 7;
console.log(command.execute());
export { saved, Checker, Command, create, command, later };
"#,
        vec![
            logical_module_with_anon(
                "command",
                &[
                    Member::new("saved"),
                    Member::new("Checker"),
                    Member::new("Command"),
                    Member::new("create"),
                    Member::new("command"),
                ],
                &[
                    "globalThis.saved = saved;",
                    "globalThis.savedChecker = Checker;",
                ],
            ),
            logical_module("value", &[Member::new("later")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "7\n");
}

fn expect_callback_reentry_for_class(checker: &str, prelude: &str) {
    let source = format!(
        r#"const saved = () => later;
globalThis.saved = saved;
{prelude}
{checker}
globalThis.savedChecker = Checker;
class Command {{
  constructor(config, checker) {{ this.config = config; this.checker = checker; }}
}}
function create(config) {{ return new Command(config, new Checker()); }}
const command = create({{ execute: () => later }});
const later = 7;
export {{ saved, Checker, Command, create, command, later }};
"#
    );
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &source,
            vec![
                logical_module("command", &[Member::new("command")]),
                logical_module("value", &[Member::new("later")]),
            ],
        ),
        &["command", "value"],
        &[],
    );
}

#[test]
fn void_effectful_and_property_operands_keep_callback_reentry() {
    expect_callback_reentry_for_class("class Checker { result = void globalThis.saved(); }", "");
    expect_callback_reentry_for_class(
        "class Checker { result = void receiver.value; }",
        "const receiver = { get value() { globalThis.saved(); } };",
    );
    expect_callback_reentry_for_class(
        "class Checker { result = void receiver[key]; }",
        r#"const receiver = { value: 1 };
const key = { [Symbol.toPrimitive]() { globalThis.saved(); return "value"; } };"#,
    );
}

#[test]
fn an_invoking_explicit_constructor_keeps_callback_reentry() {
    expect_callback_reentry_for_class(
        "class Checker { result = void 0; constructor() { globalThis.saved(); } }",
        "",
    );
}

fn assert_unproven_construction_stays_sequenced(prefix: &str, class: &str, initializer: &str) {
    let source = format!(
        r#"globalThis.events = [];
{prefix}
const a = (() => 1)();
{class}
const b = {initializer};
const c = b ? a : 0;
console.log(c);
export {{ a, b, c, Registry }};
"#
    );
    expect_sequenced_cycle_rejection(
        FixtureOpts::new(
            &source,
            vec![logical_module(
                "registry",
                &[Member::new("Registry"), Member::new("b")],
            )],
        ),
        &["registry", "residual"],
    );
}

#[test]
fn effectful_and_unknown_field_initializers_stay_sequenced() {
    for (prefix, field) in [
        ("", "globalThis.events.length"),
        (
            "const receiver = { get value() { return globalThis.events.length; } };",
            "receiver.value",
        ),
        (
            "const receiver = { valueOf() { return globalThis.events.length; } };",
            "+receiver",
        ),
    ] {
        let class = format!("class Registry {{ value = {field}; }}");
        assert_unproven_construction_stays_sequenced(prefix, &class, "new Registry()");
    }
    assert_unproven_construction_stays_sequenced(
        "",
        "class Registry { value = globalThis.events.push('init'); }",
        "new Registry()",
    );
    assert_unproven_construction_stays_sequenced(
        "const values = [];",
        "class Registry { value = new Set(values); }",
        "new Registry()",
    );
    assert_unproven_construction_stays_sequenced(
        "function Set() { globalThis.events.push('init'); }",
        "class Registry { value = new Set(); }",
        "new Registry()",
    );
}

#[test]
fn unsupported_class_surfaces_stay_sequenced() {
    for class in [
        "class Registry { constructor() { this.value = 1; } }",
        "class Registry extends Object {}",
        "class Registry { [globalThis.key] = 1; }",
        "class Registry { static value = globalThis.events.push('init'); }",
    ] {
        assert_unproven_construction_stays_sequenced("", class, "new Registry()");
    }
}

#[test]
fn rebound_class_binding_stays_sequenced() {
    let source = r#"const a = (() => 1)();
class Registry { value = 1; }
Registry = class { value = 2; };
const b = new Registry();
const c = b ? a : 0;
console.log(c);
export { a, b, c, Registry };
"#;
    let rejected = run_rejection_fixture(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "registry",
            &[Member::new("Registry"), Member::new("b")],
            &["Registry = class { value = 2; };"],
        )],
    ));
    let graph = rejected.dependency_graph();
    let constructed = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "b")
        })
        .expect("construction source unit");
    let purity = serde_json::to_value(&constructed.purity).unwrap();
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
fn constructor_arguments_stay_sequenced() {
    for initializer in ["new Registry(1)", "new Registry(...[])"] {
        assert_unproven_construction_stays_sequenced(
            "",
            "class Registry { value = 1; }",
            initializer,
        );
    }
}

#[test]
fn asserted_pure_field_calls_do_not_supply_a_heap_certificate() {
    let source = "const values = [];\nconst a = (() => 1)();\nfunction read() { const first = values.length; values.push(42); return first; }\nclass Registry { value = read(); }\nconst b = new Registry();\nconst c = b ? a : 0;\nconsole.log(c);\n";
    let rejected = run_rejection_fixture(FixtureOpts::new(
        source,
        vec![logical_module(
            "registry",
            &[
                Member::new("Registry").with_purity(MemberPurity::PureNew),
                Member::new("read").with_purity(MemberPurity::Pure),
                Member::new("b"),
            ],
        )],
    ));
    let graph = rejected.dependency_graph();
    let construction = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "b")
        })
        .unwrap();
    assert!(construction.purity.is_pure());
    assert!(graph.edges.iter().any(
        |edge| edge.source == construction.id && edge.edge_kind == analysis::DepKind::Sequenced
    ));
    assert!(!rejected.cycles().is_empty());
}

#[test]
fn certified_new_keeps_other_same_statement_constructor_and_argument_effects() {
    let source = "const values = [];\nfunction read() { globalThis.observed = values.length; return 0; }\nclass Registry { value = 1; }\nconst { missing = new Registry(read()) } = new Registry();\nvalues.push(42);\nconsole.log(globalThis.observed, missing.value);\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module("values", &[Member::new("values")]),
            logical_module(
                "a_reader",
                &[
                    Member::new("read"),
                    Member::new("Registry"),
                    Member::new("missing"),
                ],
            ),
            logical_module_with_anon("z_mutation", &[], &["values.push(42);"]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "0 1\n");
    let graph = fixture.dependency_graph();
    let mutation = graph
        .nodes
        .iter()
        .find(|node| node.statement_ordinal.0 == 4)
        .unwrap();
    let observation = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "missing")
        })
        .unwrap();
    assert!(graph.edges.iter().any(|edge| edge.source == mutation.id
        && edge.target == observation.id
        && edge.edge_kind == analysis::DepKind::Sequenced));
}

#[test]
fn certified_class_shape_keeps_earlier_binding_initialization_observations() {
    let source = "function make() { try { return new Registry().value; } catch (error) { return error.name; } }\nconst before = make();\nclass Registry { value = 42; }\nconsole.log(before);\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("maker", &[Member::new("make"), Member::new("before")]),
                logical_module("registry", &[Member::new("Registry")]),
            ],
        ),
        &["maker", "registry"],
        &[],
    );
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "ReferenceError\n");
}

#[test]
fn certified_construction_retains_deferred_field_and_getter_captures() {
    for (member, access) in [
        ("read = () => later;", "registry.read()"),
        ("get current() { return later; }", "registry.current"),
    ] {
        let source = format!(
            "const values = [];\nclass Registry {{ {member} }}\nconst registry = new Registry();\nfunction probe() {{ try {{ return {access}; }} catch (error) {{ return error.name; }} }}\nconst before = probe();\nconst later = 42;\nconsole.log(before, values.length);\n"
        );
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module(
                        "reader",
                        &[
                            Member::new("Registry"),
                            Member::new("registry"),
                            Member::new("probe"),
                            Member::new("before"),
                        ],
                    ),
                    logical_module("value", &[Member::new("later")]),
                ],
            ),
            &["reader", "value"],
            &[],
        );
        let fixture = run_fixture(FixtureOpts::new(&source, vec![]));
        assert_preserved_entry_output(&fixture, "ReferenceError 0\n");
    }
}

const CAPTURE_PROVIDER: &str = "var captured = globalThis.runtimeSettings.state;\nObject.assign(captured, { get touched() { globalThis.runtimeSettings.events.push('getter'); return true; } });\nexport const ready = true;\n";
const OPAQUE_SETUP: &str = "globalThis.runtimeSettings = { events: [], get state() { this.events.push('capture'); return { value: 'ready', child: new Map() }; } };\n";
const CAPTURE_CHUNKS: &[(&str, &str)] = &[("static/peer", CAPTURE_PROVIDER)];
const OPAQUE_FILES: &[(&str, &str)] = &[
    ("static/opaque.js", OPAQUE_SETUP),
    ("static/app/opaque.js", OPAQUE_SETUP),
];
const SET_MUTATION: &str = "Set.prototype.add = function (value) { globalThis.runtimeSettings.events.push('mutated'); return this; };";
const REBOUND_REGISTRY: &str = "Registry = class Registry { #values = new Set(); readLater = () => later; get size() { return this.#values.size; } };";
const COMPOSITION_SOURCE: &str = r#"import './opaque.js';
import { ready } from './peer.js';
const seed = (() => 1)();
const unique = new Set(['a', 'b']);
class Registry {
  #values = new Set();
  readLater = () => later;
  get size() { return this.#values.size; }
}
const registry = new Registry();
class PlainRegistry { ready = 0; }
const plain = new PlainRegistry();
const total = seed + unique.size + registry.size;
const later = 40;
console.log(ready, total, registry.readLater(), plain.ready, globalThis.runtimeSettings.events.join(','));
export { Registry, registry, unique, PlainRegistry, plain };
"#;
const COMPOSITION_OUTPUT: &str = "true 3 40 0 capture,getter\n";

fn source_hash(source: &str) -> String {
    Sha256::digest(source.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn captured_input() -> ValueInputContract {
    ValueInputContract {
        source_match: serde_yaml::from_str(&format!(
            "match: {:?}\nbindings: [captured]\n",
            "var captured = globalThis.runtimeSettings.state;"
        ))
        .expect("valid captured declaration match"),
        value: CapturedValueContract {
            kind: CapturedValueKind::OrdinaryObject,
        },
        audited_source_sha256: source_hash(CAPTURE_PROVIDER),
        expected_export: None,
        note: Some("Neutral fixture audits this captured object.".to_string()),
    }
}

fn composition_options<'a>(source: &'a str, anonymous: &'a [&'a str]) -> FixtureOpts<'a> {
    FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "registry",
            &[
                Member::new("Registry"),
                Member::new("registry"),
                Member::new("unique"),
            ],
            anonymous,
        )],
    )
    .with_extra_chunks(CAPTURE_CHUNKS)
    .with_extra_files(OPAQUE_FILES)
    .with_value_inputs("static/peer", vec![captured_input()])
}

fn source_unit<'a>(graph: &'a DependencyGraphReport, binding: &str) -> &'a SourceUnitReport {
    graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|candidate| candidate.binding == binding)
        })
        .unwrap_or_else(|| panic!("source unit for {binding} missing"))
}

fn inference_use(node: &SourceUnitReport, conclusion: InferenceRule) -> &InferenceUse {
    node.synchronous_execution
        .as_ref()
        .expect("synchronous execution report")
        .inference_uses
        .iter()
        .find(|usage| usage.conclusion == conclusion)
        .unwrap_or_else(|| panic!("{conclusion:?} inference missing from {node:?}"))
}

fn captured_input_ref() -> ValueInputRef {
    ValueInputRef {
        defining_chunk: "static/peer".to_string(),
        input_index: 0,
    }
}

fn captured_input_refs() -> BTreeSet<ValueInputRef> {
    BTreeSet::from([captured_input_ref()])
}

fn expected_purity(value_inputs: BTreeSet<ValueInputRef>) -> Purity {
    Purity::Pure {
        dependencies: ProofDependencies {
            value_inputs,
            ..Default::default()
        },
    }
}

#[test]
fn composed_native_set_and_local_class_proofs_keep_captured_input_provenance() {
    let fixture = assert_pure_cycle_break_with_opts(
        composition_options(COMPOSITION_SOURCE, &[]),
        "registry",
        &[
            "const unique = new Set",
            "class Registry",
            "const registry = new Registry",
        ],
        &["const seed", "const later"],
        COMPOSITION_OUTPUT,
    );
    assert_preserved_entry_output(&fixture, COMPOSITION_OUTPUT);

    let graph = fixture.dependency_graph();
    let unique = source_unit(&graph, "unique");
    assert_eq!(unique.purity, expected_purity(captured_input_refs()));
    let primitive_set = inference_use(unique, InferenceRule::PrimitiveSetIteration);
    assert_eq!(
        primitive_set,
        &InferenceUse {
            conclusion: InferenceRule::PrimitiveSetIteration,
            derivation_rules: BTreeSet::from([
                InferenceRule::NativeIntegrity,
                InferenceRule::PrimitiveSetIteration,
            ]),
            value_input_refs: captured_input_refs(),
            descriptor_copy_author_refs: Default::default(),
            argument_input_refs: BTreeSet::new(),
            argument_input_spec_locations: BTreeSet::new(),
            native_operation_input_refs: BTreeSet::new(),
        }
    );
    let unique_execution = unique
        .synchronous_execution
        .as_ref()
        .expect("primitive Set execution report");
    assert_eq!(
        unique_execution
            .native_integrity_uses
            .iter()
            .filter(|usage| usage.guard == NativeIntegrityGuard::SetIterable)
            .collect::<Vec<_>>(),
        vec![&NativeIntegrityUse {
            guard: NativeIntegrityGuard::SetIterable,
            value_input_refs: captured_input_refs(),
        }]
    );

    let registry = source_unit(&graph, "registry");
    assert_eq!(registry.purity, expected_purity(captured_input_refs()));
    let local_class = inference_use(registry, InferenceRule::LocalClassConstruction);
    assert_eq!(
        local_class,
        &InferenceUse {
            conclusion: InferenceRule::LocalClassConstruction,
            derivation_rules: BTreeSet::from([
                InferenceRule::NativeIntegrity,
                InferenceRule::NativeCallIdentity,
                InferenceRule::EmptyCollectionConstruction,
                InferenceRule::LocalClassConstruction,
            ]),
            value_input_refs: captured_input_refs(),
            descriptor_copy_author_refs: Default::default(),
            argument_input_refs: BTreeSet::new(),
            argument_input_spec_locations: BTreeSet::new(),
            native_operation_input_refs: BTreeSet::new(),
        }
    );
    assert_eq!(
        registry.heap_native_integrity_uses,
        vec![NativeIntegrityUse {
            guard: NativeIntegrityGuard::LocalClassConstruction,
            value_input_refs: captured_input_refs(),
        }]
    );

    let plain = source_unit(&graph, "plain");
    assert_eq!(plain.purity, expected_purity(BTreeSet::new()));
    let plain_class = inference_use(plain, InferenceRule::LocalClassConstruction);
    assert_eq!(
        plain_class,
        &InferenceUse {
            conclusion: InferenceRule::LocalClassConstruction,
            derivation_rules: BTreeSet::from([InferenceRule::LocalClassConstruction]),
            value_input_refs: BTreeSet::new(),
            descriptor_copy_author_refs: Default::default(),
            argument_input_refs: BTreeSet::new(),
            argument_input_spec_locations: BTreeSet::new(),
            native_operation_input_refs: BTreeSet::new(),
        }
    );
    assert!(plain.heap_native_integrity_uses.is_empty());
}

#[test]
fn composed_proofs_reject_native_set_mutation_and_unsafe_field_initializers() {
    let mutated = COMPOSITION_SOURCE.replace(
        "const unique = new Set(['a', 'b']);",
        &format!("{SET_MUTATION}\nconst unique = new Set(['a', 'b']);"),
    );
    let mutated_fixture = run_fixture(
        FixtureOpts::new(&mutated, vec![])
            .with_extra_chunks(CAPTURE_CHUNKS)
            .with_extra_files(OPAQUE_FILES)
            .with_value_inputs("static/peer", vec![captured_input()]),
    );
    assert_preserved_entry_output(
        &mutated_fixture,
        "true 1 40 0 capture,getter,mutated,mutated\n",
    );
    // The adder write reaches `new Set(['a', 'b'])`, which calls it, but not
    // the empty `#values = new Set()`, which never reads it.
    let graph =
        run_rejection_fixture(composition_options(&mutated, &[SET_MUTATION])).dependency_graph();
    assert!(
        !source_unit(&graph, "unique")
            .synchronous_execution
            .as_ref()
            .expect("Set construction execution report")
            .complete
    );
    inference_use(
        source_unit(&graph, "registry"),
        InferenceRule::LocalClassConstruction,
    );

    let unsafe_field = COMPOSITION_SOURCE.replace(
        "#values = new Set();",
        "#values = (globalThis.runtimeSettings.events.push('field'), new Set());",
    );
    let unsafe_fixture = run_fixture(
        FixtureOpts::new(&unsafe_field, vec![])
            .with_extra_chunks(CAPTURE_CHUNKS)
            .with_extra_files(OPAQUE_FILES)
            .with_value_inputs("static/peer", vec![captured_input()]),
    );
    assert_preserved_entry_output(&unsafe_fixture, "true 3 40 0 capture,getter,field\n");
    expect_atomic_conflict_rejection(
        composition_options(&unsafe_field, &[]),
        &["registry", "residual"],
        &[],
    );
}

#[test]
fn composed_proof_rejects_rebound_class_bindings() {
    let source = COMPOSITION_SOURCE.replace(
        "const registry = new Registry();",
        &format!("{REBOUND_REGISTRY}\nconst registry = new Registry();"),
    );
    let fixture = run_fixture(
        FixtureOpts::new(&source, vec![])
            .with_extra_chunks(CAPTURE_CHUNKS)
            .with_extra_files(OPAQUE_FILES)
            .with_value_inputs("static/peer", vec![captured_input()]),
    );
    assert_preserved_entry_output(&fixture, COMPOSITION_OUTPUT);

    let rejected = run_rejection_fixture(composition_options(&source, &[REBOUND_REGISTRY]));
    let graph = rejected.dependency_graph();
    let construction = source_unit(&graph, "registry");
    let Purity::NotPure { reasons } = &construction.purity else {
        panic!("a rebound class must not retain the implicit-construction proof: {construction:?}");
    };
    assert!(
        reasons
            .iter()
            .any(|reason| reason.rule == analysis::PurityRule::UnknownNew)
    );
}

#[test]
fn callback_invoked_by_a_field_initializer_keeps_the_real_tdz_rejection() {
    let source = r#"globalThis.saved = () => later;
function invoke(callback) { return callback(); }
class Registry { value = invoke(globalThis.saved); }
let registry;
try { registry = new Registry(); } catch (error) { registry = error.name; }
const later = 40;
console.log(registry);
export { Registry, registry, later };
"#;
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "ReferenceError\n");

    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "registry",
                    &[Member::new("Registry"), Member::new("registry")],
                    &["globalThis.saved = () => later;"],
                ),
                logical_module("value", &[Member::new("later")]),
            ],
        ),
        &["registry", "value"],
        &[],
    );
}
