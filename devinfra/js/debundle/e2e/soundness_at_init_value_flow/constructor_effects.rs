//! Constructors and instance fields that can run callbacks constrain order; stored callbacks stay lazy.

use super::{command_later_split, in_case};
use debundle_e2e_support::*;

/// Constructor shapes whose `new` may run the config callback (or code the
/// analysis cannot see) before `later` initializes.
#[test]
fn command_split_rejects_constructors_that_may_run_callbacks() {
    for (name, source) in [
        (
            "reassigned escaped class keeps the unknown constructor alternative",
            r#"const saved = () => later;
globalThis.saved = saved;
class Checker { isAllowed() { return true; } }
globalThis.savedChecker = Checker;
Checker = function () { saved(); };
class Command {
  constructor(config, checker) { this.config = config; this.checker = checker; }
}
function create(config) { return new Command(config, new Checker()); }
const command = create({ execute: () => later });
const later = 7;
export { saved, Checker, Command, create, command, later };
"#,
        ),
        (
            "opaque checker receiving the config",
            r#"class Checker { constructor(config) { config.execute(); } }
globalThis.savedChecker = Checker;
class Command {
  constructor(config, checker) { this.config = config; this.checker = checker; }
}
function create(config) { return new Command(config, new Checker(config)); }
const command = create({ execute: () => later });
const later = 7;
export { Checker, Command, create, command, later };
"#,
        ),
        (
            "prototype getter collides with the stored field",
            r#"class Command {
  get config() { return 0; }
  constructor(config) { this.config = config; }
}
const command = new Command({ execute: () => later });
const later = 7;
export { Command, command, later };
"#,
        ),
        (
            "constructor invokes the callback",
            r#"class Command {
  constructor(config) { config.execute(); }
}
function factory(config) { return new Command(config); }
const command = factory({ execute: () => later });
const later = 7;
export { Command, factory, command, later };
"#,
        ),
        (
            "prototype setter receives the config",
            r#"class Command {
  set config(value) { value.execute(); }
  constructor(config) { this.config = config; }
}
function factory(config) { return new Command(config); }
const command = factory({ execute: () => later });
const later = 7;
export { Command, factory, command, later };
"#,
        ),
        (
            "config getter read by the constructor",
            r#"class Command {
  constructor(config) { this.weight = config.weight ?? 0; }
}
const command = new Command({ get weight() { return later; } });
const later = 7;
export { Command, command, later };
"#,
        ),
        (
            "derived constructor",
            r#"class Base {}
class Command extends Base {
  constructor(config) { super(); this.config = config; }
}
function factory(config) { return new Command(config); }
const command = factory({ execute: () => later });
const later = 7;
export { Base, Command, factory, command, later };
"#,
        ),
        (
            "instance field calls before the later binding",
            r#"class Command { result = execute(); }
function execute() { return later; }
const command = new Command();
const later = 7;
export { Command, execute, command, later };
"#,
        ),
        (
            "constructor returns another object",
            r#"class Command {
  constructor(config) { return { execute: config.execute }; }
}
function factory(config) { return new Command(config); }
const command = factory({ execute: () => later });
const later = 7;
export { Command, factory, command, later };
"#,
        ),
    ] {
        in_case(name, || {
            expect_atomic_conflict_rejection(
                command_later_split(source),
                &["command", "value"],
                &[],
            );
        });
    }
}

/// Anchor for the rejected instance-field call: with `later` declared first
/// the same field call is safe.
#[test]
fn instance_field_initializer_runs_when_constructed() {
    let fixture = run_fixture(command_later_split(
        r#"const later = 7;
function execute() { return later; }
class Command { result = execute(); }
const command = new Command();
console.log(command.result);
export { Command, execute, command, later };
"#,
    ));
    assert_preserved_entry_output(&fixture, "7\n");
}

#[test]
fn instance_field_that_stores_callback_remains_lazy() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"class Command { execute = () => later; }
const command = new Command();
const later = 7;
console.log(command.execute());
export { Command, command, later };
"#,
            vec![
                logical_module("command", &binding_members(["Command", "command"])),
                logical_module("value", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7\n");
}

#[test]
fn published_instance_can_expose_stored_callback() {
    expect_atomic_conflict_rejection(
        command_later_split(
            r#"class Command { constructor(config) { this.config = config; } }
function factory(config) { return new Command(config); }
const command = factory({ execute: () => later });
globalThis.saved = command;
console.log("ready");
const later = 7;
export { Command, factory, command, later };
"#,
        ),
        &["anon_residual_sentinel", "value"],
        &[],
    );
}

// ─── factories returning closures ───────────────────────────────────────
