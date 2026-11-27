//! Local constructors store config callbacks without invoking them and stay conservative for other shapes.

use super::{command_later_split, in_case};
use debundle_e2e_support::*;

/// `Command`, `factory` and `command` in one module, `later` in another.
fn command_factory_split(source: &str) -> FixtureOpts<'_> {
    FixtureOpts::new(
        source,
        vec![
            logical_module(
                "command",
                &binding_members(["Command", "factory", "command"]),
            ),
            logical_module("value", &[Member::new("later")]),
        ],
    )
    .with_unassigned_mode(unassigned_mode_inline())
}

/// A local constructor that stores the config callback or reads plain data
/// from a fresh config never runs `execute` before `later` initializes.
#[test]
fn local_constructor_defers_callbacks_stored_from_config() {
    for (name, source, expected) in [
        (
            "stores the config until called",
            r#"class Command {
  constructor(config) { this.config = config; }
  execute() { return this.config.execute(); }
}
function factory(config) { return new Command(config); }
const command = factory({ execute: () => later });
const later = 7;
console.log(command.execute());
export { Command, factory, command, later };
"#,
            "7\n",
        ),
        (
            "reads plain config data",
            r#"class Command {
  constructor(config) { this.execute = config.execute; }
}
function factory(config) { return new Command(config); }
const command = factory({ execute: () => later });
const later = 7;
console.log(command.execute());
export { Command, factory, command, later };
"#,
            "7\n",
        ),
        (
            "reads absent plain config data",
            r#"class Command {
  constructor(config) {
    this.execute = config.execute;
    this.weight = config.weight ?? 0;
  }
}
function factory(config) { return new Command(config); }
const command = factory({ execute: () => later });
const later = 7;
console.log(command.execute(), command.weight);
export { Command, factory, command, later };
"#,
            "7 0\n",
        ),
        (
            "fresh config with an ordinary method",
            r#"class Command {
  constructor(config) {
    this.execute = config.execute;
    this.isAllowed = config.isAllowed;
    this.weight = config.weight ?? 0;
    this.focus = config.focus === !0;
  }
}
function factory(config) { return new Command(config); }
const command = factory({ isAllowed() { return true; }, execute: () => later });
const later = 7;
console.log(command.isAllowed(), command.weight, command.focus, command.execute());
export { Command, factory, command, later };
"#,
            "true 0 false 7\n",
        ),
    ] {
        in_case(name, || {
            let fixture = run_fixture(command_factory_split(source));
            assert_preserved_entry_output(&fixture, expected);
        });
    }
}

#[test]
fn local_constructor_callback_field_remains_a_consumer() {
    let source = r#"let later = 0;
class Holder {
  constructor(callback) { this.value = callback; }
}
const holder = new Holder(() => later);
globalThis.observed = holder.value();
later = 42;
console.log(globalThis.observed, later);
"#;
    let colocated = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "all",
                &binding_members(["Holder", "holder", "later"]),
                &[
                    "globalThis.observed = holder.value();",
                    "later = 42;",
                    "console.log(globalThis.observed, later);",
                ],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&colocated, "0 42\n");

    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("command", &binding_members(["Holder", "holder"])),
                logical_module_with_anon(
                    "consumer",
                    &[],
                    &["globalThis.observed = holder.value();"],
                ),
                logical_module_with_anon(
                    "value",
                    &[Member::new("later")],
                    &["later = 42;", "console.log(globalThis.observed, later);"],
                ),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["consumer", "value"],
        &[],
    );
}

#[test]
fn local_constructor_keeps_non_data_config_shapes_conservative() {
    let template = r#"class Command {
  constructor(config) {
    this.execute = config.execute;
    this.weight = config.weight ?? 0;
  }
}
function factory(config) { return new Command(config); }
const command = factory(CONFIG);
const later = 7;
export { Command, factory, command, later };
"#;
    for config in [
        r#"{ ["isAllowed"]() { return true; }, execute: () => later }"#,
        r#"{ ...{}, isAllowed() { return true; }, execute: () => later }"#,
        r#"{ get weight() { return 0; }, isAllowed() { return true; }, execute: () => later }"#,
        r#"{ set unused(value) {}, isAllowed() { return true; }, execute: () => later }"#,
        r#"new Proxy({ isAllowed() { return true; }, execute: () => later }, {})"#,
    ] {
        in_case(config, || {
            let source = template.replace("CONFIG", config);
            expect_atomic_conflict_rejection(
                command_later_split(&source),
                &["command", "value"],
                &[],
            );
        });
    }
}

#[test]
fn local_constructor_method_config_keeps_actual_early_reads() {
    let template = r#"let later = 0;
class Command {
  constructor(config) { CONSTRUCTOR_BODY }
}
function factory(config) { return new Command(config); }
const command = factory(CONFIG);
later = 7;
console.log(globalThis.observed, command.execute());
export { Command, factory, command, later };
"#;
    for (constructor_body, config) in [
        (
            "globalThis.observed = config.execute(); this.execute = config.execute;",
            "{ isAllowed() { return true; }, execute: () => later }",
        ),
        (
            "this.execute = config.execute; this.weight = config.weight ?? 0;",
            "{ isAllowed() { return true; }, get weight() { globalThis.observed = later; return 0; }, execute: () => later }",
        ),
    ] {
        in_case(config, || {
            let source = template
                .replace("CONSTRUCTOR_BODY", constructor_body)
                .replace("CONFIG", config);
            let colocated = run_fixture(
                FixtureOpts::new(
                    &source,
                    vec![logical_module_with_anon(
                        "all",
                        &binding_members(["Command", "factory", "command", "later"]),
                        &["later = 7;"],
                    )],
                )
                .with_unassigned_mode(unassigned_mode_inline()),
            );
            assert_preserved_entry_output(&colocated, "0 7\n");
            expect_atomic_conflict_rejection(
                FixtureOpts::new(
                    &source,
                    vec![
                        logical_module(
                            "command",
                            &binding_members(["Command", "factory", "command"]),
                        ),
                        logical_module_with_anon("value", &[Member::new("later")], &["later = 7;"]),
                    ],
                )
                .with_unassigned_mode(unassigned_mode_inline()),
                &["command", "value"],
                &[],
            );
        });
    }
}

#[test]
fn local_constructor_sequence_and_lazy_fields_keep_callbacks_deferred() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"class Command {
  constructor(config, checker, attrs) {
    (this.config = config, this.checker = checker, this.attrs = attrs,
     this.weight = config.weight ?? 0, this.focus = config.focus === !0,
     this.children = config.children);
  }
  weight;
  focus;
  children;
  search = (context) => this.execute(context);
  get expands() { return this.children; }
  execute(context) { return this.config.execute(context); }
}

function factory(config, checker, attrs) { return new Command(config, checker, attrs); }
function isCommand(value) { return value instanceof Command; }
const command = factory({ execute: () => later }, () => later, {});
const later = 7;
console.log(isCommand(command), command.focus, command.search());
export { Command, factory, isCommand, command, later };
"#,
            vec![
                logical_module(
                    "command",
                    &binding_members(["Command", "factory", "isCommand", "command"]),
                ),
                logical_module("value", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "true false 7\n");
}

#[test]
fn fresh_constructor_writes_after_opaque_checker_keep_callback_deferred() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"class Checker { isAllowed() { return true; } }
globalThis.savedChecker = Checker;
class Command {
  constructor(config, checker) {
    this.config = config;
    this.checker = checker;
    this.weight = config.weight ?? 0;
  }
  execute() { return this.config.execute(); }
}
function create(config) { return new Command(config, new Checker()); }
const command = create({ execute: () => later });
const later = 7;
console.log(command.execute());
export { Checker, Command, create, command, later };
"#,
            vec![
                logical_module_with_anon(
                    "command",
                    &binding_members(["Checker", "Command", "create", "command"]),
                    &["globalThis.savedChecker = Checker;"],
                ),
                logical_module("value", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7\n");
}
