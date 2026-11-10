//! Report-only effect translation validation: `new` of a local class through
//! the kernel's construct rule. The checked class body (instance fields, then
//! the constructor's statements) runs against the fresh instance; refused
//! constructions keep their typed legacy reason.

use debundle_e2e_graph_support::effect_validation::*;
use debundle_e2e_support::*;

/// `class` at the top, an independent declaration, then the construction:
/// extracting the class with its instance inverts the two.
fn construction_source(class: &str, construction: &str, print: &str) -> String {
    format!(
        "{class}\nconst independent = 7;\nconst registry = {construction};\nconsole.log(independent, {print});\n"
    )
}

fn construction_fixture(source: &str) -> Fixture {
    run_fixture(FixtureOpts::new(
        source,
        vec![logical_module(
            "registry",
            &[Member::new("Registry"), Member::new("registry")],
        )],
    ))
}

fn assert_construction_proved(fixture: &Fixture, case: &str) {
    assert_inline_operation_receipt(fixture, "registry", 1, case);
    let execution = binding_execution_report(fixture, "registry");
    let call = &execution["binding_effect_authority"]["receipt"]["calls"][0];
    assert_eq!(call["target"]["kind"], "constructor", "{case}: {call:#}");
    let report = fixture.effect_translation_validation();
    assert_eq!(report["swapped_pairs"], 1, "{case}: {report:#}");
    assert_eq!(
        report["decision"]["status"], "accepted",
        "{case}: {report:#}"
    );
    assert_eq!(
        report["decision"]["scope"], "statement_order_commutation",
        "{case}: {report:#}"
    );
}

/// The checked constructor refused this class with `check`; the
/// construction owner gets no operation receipt.
fn assert_construction_refused(fixture: &Fixture, check: &str, case: &str) {
    let execution = binding_execution_report(fixture, "registry");
    let losses = execution["precision_losses"]
        .as_array()
        .expect("precision losses");
    assert!(
        losses.iter().any(|loss| {
            loss["reason"]["code"] == "unsupported_construction"
                && loss["reason"]["rejection"]["check"] == check
        }),
        "{case}: the refusal must be the typed {check} rejection: {losses:#?}"
    );
    assert_ne!(
        execution["binding_effect_authority"]["authority"], "operation_receipt",
        "{case}: {execution:#}"
    );
}

#[test]
fn checked_instance_fields_are_proved_across_an_independent_inversion() {
    for (class, print, expected) in [
        (
            "class Registry { limit = 0; items = []; }",
            "registry.limit, registry.items.length",
            "7 0 0\n",
        ),
        (
            "class Registry { label = 'idle'; ready = true; }",
            "registry.label, registry.ready",
            "7 idle true\n",
        ),
        (
            "class Registry { #count = 0; get count() { return this.#count; } }",
            "registry.count",
            "7 0\n",
        ),
    ] {
        let source = construction_source(class, "new Registry()", print);
        let fixture = construction_fixture(&source);
        assert_preserved_entry_output(&fixture, expected);
        assert_construction_proved(&fixture, class);
    }
}

#[test]
fn constructor_parameter_stores_are_proved_across_an_independent_inversion() {
    for (class, construction, print, expected) in [
        (
            "class Registry { constructor(limit) { this.limit = limit; } }",
            "new Registry(3)",
            "registry.limit",
            "7 3\n",
        ),
        (
            "class Registry { constructor(limit, label) { this.limit = limit; this.label = label; } }",
            "new Registry(3, 'main')",
            "registry.limit, registry.label",
            "7 3 main\n",
        ),
        (
            "class Registry { constructor(limit) { this.limit = limit; } add(value) { return value + this.limit; } }",
            "new Registry(3)",
            "registry.add(2)",
            "7 5\n",
        ),
    ] {
        let source = construction_source(class, construction, print);
        let fixture = construction_fixture(&source);
        assert_preserved_entry_output(&fixture, expected);
        assert_construction_proved(&fixture, class);
    }
}

#[test]
fn methods_and_deferred_field_callbacks_do_not_run_at_construction() {
    for (class, print, expected) in [
        (
            "class Registry { add(value) { return value; } get size() { return 0; } }",
            "registry.add(2), registry.size",
            "7 2 0\n",
        ),
        (
            "class Registry { read = () => 4; }",
            "registry.read()",
            "7 4\n",
        ),
    ] {
        let source = construction_source(class, "new Registry()", print);
        let fixture = construction_fixture(&source);
        assert_preserved_entry_output(&fixture, expected);
        assert_construction_proved(&fixture, class);
    }
}

/// Static members are evaluated with the class definition, outside the
/// checked constructor's shape: the construction stays refused.
#[test]
fn static_members_keep_the_unchecked_constructor_refusal() {
    let source = construction_source(
        "class Registry { static count = 0; items = []; }",
        "new Registry()",
        "Registry.count, registry.items.length",
    );
    let fixture = construction_fixture(&source);
    assert_preserved_entry_output(&fixture, "7 0 0\n");
    assert_construction_refused(&fixture, "unchecked_constructor", "static field");
}

#[test]
fn derived_and_invoking_constructors_keep_the_unchecked_constructor_refusal() {
    for (class, construction, print, expected) in [
        (
            "class Base {}\nclass Registry extends Base { limit = 0; }",
            "new Registry()",
            "registry.limit",
            "7 0\n",
        ),
        (
            "function initial() { return 2; }\nclass Registry { constructor() { this.limit = initial(); } }",
            "new Registry()",
            "registry.limit",
            "7 2\n",
        ),
    ] {
        let source = construction_source(class, construction, print);
        let fixture = construction_fixture(&source);
        assert_preserved_entry_output(&fixture, expected);
        assert_construction_refused(&fixture, "unchecked_constructor", class);
    }
}

/// A class read outside `new` may have its prototype changed by its reader,
/// so a constructor store can reach a setter.
#[test]
fn a_class_used_outside_construction_keeps_its_refusal() {
    let source = "class Registry { constructor(limit) { this.limit = limit; } }\nconst alias = Registry;\nconst independent = 7;\nconst registry = new Registry(3);\nconsole.log(independent, registry.limit, alias === Registry);\n";
    let fixture = construction_fixture(source);
    assert_preserved_entry_output(&fixture, "7 3 true\n");
    assert_construction_refused(&fixture, "class_used_outside_construction", "aliased class");
}

/// A factory callable runs on the flat lane's construction model: the
/// kernel lowers `new` only in root initializers so far.
#[test]
fn a_factory_construction_is_pending_callable_construct_admission() {
    let source = "class Registry { limit = 0; }\nfunction create() { return new Registry(); }\nconst independent = 7;\nvar registry = create();\nconsole.log(independent, registry.limit);\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module(
            "registry",
            &[
                Member::new("Registry"),
                Member::new("create"),
                Member::new("registry"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "7 0\n");
    assert_effect_verdict_pending(&fixture, "incomplete", "factory construction");
}

/// A constructor reading a parameter's property needs the flat lane's
/// checked plain-data input proof, which the kernel's own-data read lacks.
#[test]
fn a_constructor_reading_an_argument_property_is_pending_an_input_proof() {
    let source = construction_source(
        "class Registry { constructor(options) { this.limit = options.limit; } }",
        "new Registry({ limit: 3 })",
        "registry.limit",
    );
    let fixture = construction_fixture(&source);
    assert_preserved_entry_output(&fixture, "7 3\n");
    assert_effect_verdict_pending(&fixture, "incomplete", "argument property read");
}
