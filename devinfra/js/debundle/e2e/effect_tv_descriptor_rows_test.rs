//! Report-only effect translation validation: descriptor and integrity rows
//! admitted through the native plan/commit path cover their owners.

use debundle_e2e_graph_support::effect_validation::*;
use debundle_e2e_support::*;

/// A private module-namespace facade: a null-prototype literal tagged
/// through a symbol-keyed `Object.defineProperty`, then frozen.
const FACADE: &str = "const facade = Object.freeze(Object.defineProperty({__proto__: null, callback: () => marker}, Symbol.toStringTag, {value: 'Module'}));";

fn marker_first(
    declaration: &str,
    module: &'static str,
    binding: &'static str,
    log: &str,
) -> Fixture {
    let source = format!("const marker = 1;\n{declaration}\nconsole.log({log}, marker);\n");
    run_fixture(
        FixtureOpts::new(
            &source,
            vec![logical_module(module, &[Member::new(binding)])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    )
}

#[test]
fn frozen_facade_relocates_before_a_primitive_marker() {
    let fixture = marker_first(
        FACADE,
        "namespace/facade",
        "facade",
        "facade.callback(), Object.prototype.toString.call(facade)",
    );
    assert_preserved_entry_output(&fixture, "1 [object Module] 1\n");
    assert_effect_verdict_accepts(&fixture, "frozen facade");
}

#[test]
fn frozen_statement_literal_relocates_before_a_primitive_marker() {
    let fixture = marker_first(
        "const frozen = Object.freeze({ value: 2, read: () => marker });",
        "records/frozen",
        "frozen",
        "frozen.value, frozen.read(), Object.isFrozen(frozen)",
    );
    assert_preserved_entry_output(&fixture, "2 1 true 1\n");
    assert_effect_verdict_accepts(&fixture, "frozen literal");
}

#[test]
fn symbol_keyed_definition_relocates_before_a_primitive_marker() {
    let fixture = marker_first(
        "const tagged = Object.defineProperty({ value: 2 }, Symbol.toStringTag, { value: 'Tagged' });",
        "records/tagged",
        "tagged",
        "Object.prototype.toString.call(tagged), tagged.value",
    );
    assert_preserved_entry_output(&fixture, "[object Tagged] 2 1\n");
    assert_effect_verdict_accepts(&fixture, "symbol-keyed definition");
}

/// The facade's stored callback is not published: a later opaque host call
/// cannot reach it, so the facade may leave the startup module. The effect
/// verdict still waits on the opaque `Date` construction in `startup`.
#[test]
fn private_facade_split_from_an_opaque_startup_call() {
    let source = "const facade = Object.freeze(Object.defineProperty({__proto__: null, callback: () => later}, Symbol.toStringTag, {value: 'Module'}));\nconst startup = new globalThis.Date();\nconst later = 42;\nconsole.log(facade.callback(), later);\n";
    let mut split = FixtureOpts::new(
        source,
        vec![
            logical_module("state/facade", &[Member::new("facade")]),
            logical_module("startup", &[Member::new("startup")]),
            logical_module("state/later", &[Member::new("later")]),
        ],
    );
    split.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    let fixture = run_fixture(split);
    assert_preserved_entry_output(&fixture, "42 42\n");
    assert_effect_verdict_pending(&fixture, "incomplete", "facade split from opaque startup");
}

/// Freezing an object another statement already holds is not admitted: the
/// attribute change would have to be ordered against that statement's
/// writes, so the effect verdict cannot cover the relocated pair yet.
#[test]
fn freezing_an_earlier_statement_object_awaits_coverage() {
    let source = "const marker = 1;\nconst shared = { value: 0 };\nconst frozen = Object.freeze(shared);\nconsole.log(Object.isFrozen(shared), frozen === shared, marker);\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module(
                "records/frozen",
                &[Member::new("shared"), Member::new("frozen")],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "true true 1\n");
    assert_effect_verdict_pending(
        &fixture,
        "incomplete",
        "freeze of an earlier statement's object",
    );
}

/// A descriptor record with an accessor field runs user code inside
/// `ToPropertyDescriptor`; the row refuses and the split stays conservative.
#[test]
fn accessor_descriptor_field_keeps_the_facade_conservative() {
    let source = "let later = 0;\nconst facade = Object.freeze(Object.defineProperty({__proto__: null, callback: () => later}, Symbol.toStringTag, {get value() { globalThis.observed = later; return 'Module'; }}));\nconst startup = new globalThis.Date();\nlater = 42;\nconsole.log(globalThis.observed, later);\n";
    let mut colocated = FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "state/all",
            &[
                Member::new("facade"),
                Member::new("startup"),
                Member::new("later"),
            ],
            &["later = 42;"],
        )],
    );
    colocated.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    assert_preserved_entry_output(&run_fixture(colocated), "0 42\n");
    let mut split = FixtureOpts::new(
        source,
        vec![
            logical_module("state/facade", &[Member::new("facade")]),
            logical_module("startup", &[Member::new("startup")]),
            logical_module_with_anon("state/later", &[Member::new("later")], &["later = 42;"]),
        ],
    );
    split.host_behavior.synchronous_module_loader = SynchronousModuleLoader::None;
    expect_atomic_conflict_rejection(split, &["state/facade", "state/later"], &[]);
}
