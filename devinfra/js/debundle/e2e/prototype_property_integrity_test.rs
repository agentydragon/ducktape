//! Prototype-property effects must preserve private constructor callbacks only
//! when the write is proved disjoint from the property the constructor reads.

use debundle_e2e_support::*;

const CLASSES: &str = r#"class Holder {
  constructor(record, policy, callback) {
    this.record = record;
    this.policy = policy;
    this.callback = callback;
    this.weight = record.weight ?? 0;
  }
  run() { return this.record.execute() + this.callback(); }
}
class Policy { allowed() { return true; } }
"#;

const FACTORY: &str =
    "function create(record) { return new Holder(record, new Policy(), () => later); }\n";
const PREFIX_BINDING: &str = "const prefix = new globalThis.Date();\n";
const COMMAND: &str = "const command = create({ execute: () => later });\n";
const LATER: &str = "const later = 42;\n";
const OUTPUT: &str = "console.log(command.run(), command.weight);\n";

fn source(
    prefix: &str,
    factory: &str,
    after_factory: &str,
    before_later: &str,
    suffix: &str,
) -> String {
    format!(
        "{prefix}{CLASSES}{factory}{after_factory}{PREFIX_BINDING}{COMMAND}{before_later}{LATER}{OUTPUT}{suffix}"
    )
}

fn split_modules(include_borrowed: bool) -> Vec<LogicalModuleEntry> {
    let mut wrapped = vec![
        Member::new("Holder"),
        Member::new("Policy"),
        Member::new("create"),
        Member::new("prefix"),
        Member::new("command"),
    ];
    if include_borrowed {
        wrapped.push(Member::new("borrowed"));
    }
    vec![
        logical_module("callbacks/wrapped", &wrapped),
        logical_module("values/later", &[Member::new("later")]),
    ]
}

fn assert_preserved_split(prefix: &str) {
    let source = source(prefix, FACTORY, "", "", "");
    let fixture = run_fixture(FixtureOpts::new(&source, split_modules(false)));
    assert_preserved_entry_output(&fixture, "84 0\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/callbacks/wrapped.js",
        &["command"],
        &["later"],
    );
}

fn assert_split_rejected(source: &str, extra_chunks: &[(&str, &str)]) {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(source, split_modules(false)).with_extra_chunks(extra_chunks),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

fn assert_effect_rejected(effect: &str) {
    let source = source(effect, FACTORY, "", "", "");
    assert_split_rejected(&source, &[]);
}

fn assert_later_conflict(source: &str, include_borrowed: bool) {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(source, split_modules(include_borrowed)),
        &["values/later"],
        &[],
    );
}

#[test]
fn deferred_object_entries_replacement_preserves_the_later_callback_split() {
    assert_preserved_split("function mutate(value) { Object.entries = value; }\n");
}

#[test]
fn unrelated_global_receiver_write_preserves_the_later_callback_split() {
    assert_preserved_split("function mutate(value) { globalThis.slot.marker = value; }\n");
}

#[test]
fn control_without_a_write_preserves_the_later_callback_split() {
    assert_preserved_split("");
}

#[test]
fn inherited_getter_on_the_read_property_remains_rejected() {
    assert_effect_rejected(
        "Object.defineProperty(Object.prototype, 'weight', { configurable: true, get() { return later; } });\n",
    );
}

#[test]
fn inherited_setter_on_the_constructor_field_remains_rejected() {
    assert_effect_rejected(
        "Object.defineProperty(Object.prototype, 'callback', { configurable: true, set(value) { void later; } });\n",
    );
}

#[test]
fn aliased_object_define_property_remains_rejected() {
    assert_effect_rejected(
        "const define = Object.defineProperty;\ndefine(Object.prototype, 'weight', { configurable: true, get() { return later; } });\n",
    );
}

#[test]
fn aliased_reflect_define_property_remains_rejected() {
    assert_effect_rejected(
        "const define = Reflect.defineProperty;\ndefine(Object.prototype, 'weight', { configurable: true, get() { return later; } });\n",
    );
}

#[test]
fn legacy_inherited_getter_remains_rejected() {
    assert_effect_rejected(
        "Object.prototype.__defineGetter__('weight', function() { return later; });\n",
    );
}

#[test]
fn legacy_inherited_setter_remains_rejected() {
    assert_effect_rejected(
        "Object.prototype.__defineSetter__('callback', function(value) { return later; });\n",
    );
}

#[test]
fn cross_chunk_descriptor_mutation_remains_rejected() {
    const MUTATOR: &str = "export function install(target, getter) {\n\
  Object.defineProperty(target, 'weight', { configurable: true, get: getter });\n\
}\n";
    let source = source(
        "import { install } from './mutator.js';\ninstall(Object.prototype, () => later);\n",
        FACTORY,
        "",
        "",
        "",
    );
    assert_split_rejected(&source, &[("static/mutator", MUTATOR)]);
}

#[test]
fn unknown_computed_descriptor_key_remains_rejected() {
    assert_effect_rejected(
        "const key = globalThis.propertyName || 'weight';\n\
         Object.defineProperty(Object.prototype, key, { configurable: true, get() { return later; } });\n",
    );
}

#[test]
fn dynamic_legacy_accessor_method_remains_rejected() {
    assert_effect_rejected(
        "const methodName = globalThis.methodName || '__defineGetter__';\n\
         Object.prototype[methodName]('weight', function() { return later; });\n",
    );
}

#[test]
fn descriptor_mutation_after_await_remains_rejected() {
    let source = source(
        "async function install() {\n\
           await 0;\n\
           Object.defineProperty(Object.prototype, 'weight', { configurable: true, get() { return later; } });\n\
         }\n\
         install();\n",
        FACTORY,
        "",
        "Promise.resolve().then(() => console.log(command.record.weight));\n",
        "",
    );
    assert_split_rejected(&source, &[]);
}

#[test]
fn inherited_data_callback_remains_rejected() {
    let source = source(
        "Object.prototype.weight = () => later;\n",
        FACTORY,
        "",
        "command.weight();\n",
        "",
    );
    assert_split_rejected(&source, &[]);
}

#[test]
fn derived_public_field_on_prototype_remains_rejected() {
    let source = source(
        "class Base { constructor() { return Object.prototype; } }\n\
         class Install extends Base { weight = () => later; }\n\
         new Install();\n",
        FACTORY,
        "",
        "command.weight();\n",
        "",
    );
    assert_split_rejected(&source, &[]);
}

#[test]
fn nested_native_capability_passed_to_foreign_helper_remains_rejected() {
    const MUTATOR: &str = "export function install(record) {\n\
      record.native.defineProperty(record.target, 'weight', { configurable: true, get: record.callback });\n\
    }\n";
    let source = source(
        "import { install } from './mutator.js';\n\
         install({ native: Object, target: Object.prototype, callback: () => later });\n",
        FACTORY,
        "",
        "",
        "",
    );
    assert_split_rejected(&source, &[("static/mutator", MUTATOR)]);
}

#[test]
fn special_proto_assignment_remains_rejected() {
    assert_effect_rejected("Object.prototype.__proto__ = { get weight() { return later; } };\n");
}

#[test]
fn borrowed_constructor_alias_remains_rejected() {
    let source = source(
        "",
        FACTORY,
        "const borrowed = Holder;\n",
        "host(borrowed, command.callback);\n",
        "",
    );
    assert_later_conflict(&source, true);
}

#[test]
fn exposed_constructor_remains_rejected() {
    let source = source(
        "",
        FACTORY,
        "globalThis.constructorHandle = Holder;\n",
        "host(globalThis.constructorHandle, command.callback);\n",
        "",
    );
    assert_later_conflict(&source, false);
}

#[test]
fn exposed_constructor_result_remains_rejected() {
    let source = source(
        "",
        FACTORY,
        "",
        "globalThis.commandHandle = command;\n\
         globalThis.commandHandle.run();\n",
        "",
    );
    assert_later_conflict(&source, false);
}
