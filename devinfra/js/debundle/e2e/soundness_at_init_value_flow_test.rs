//! At-init calls must account for reads reached through function values.
//! Each input runs successfully as one ESM chunk. An accepted split must
//! preserve that result, even when a call crosses a parameter or `new`.

use debundle_e2e_support::*;
use std::fmt::Write as _;

#[test]
fn local_constructor_stores_callback_until_called() {
    let fixture = run_fixture(
        FixtureOpts::new(
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
            vec![
                logical_module(
                    "command",
                    &[
                        Member::new("Command"),
                        Member::new("factory"),
                        Member::new("command"),
                    ],
                ),
                logical_module("value", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7\n");
}

#[test]
fn local_constructor_reads_plain_config_data_without_invoking_callback() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"class Command {
  constructor(config) { this.execute = config.execute; }
}
function factory(config) { return new Command(config); }
const command = factory({ execute: () => later });
const later = 7;
console.log(command.execute());
export { Command, factory, command, later };
"#,
            vec![
                logical_module(
                    "command",
                    &[
                        Member::new("Command"),
                        Member::new("factory"),
                        Member::new("command"),
                    ],
                ),
                logical_module("value", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7\n");
}

#[test]
fn local_constructor_reads_absent_plain_config_data_without_invoking_callback() {
    let fixture = run_fixture(
        FixtureOpts::new(
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
            vec![
                logical_module(
                    "command",
                    &[
                        Member::new("Command"),
                        Member::new("factory"),
                        Member::new("command"),
                    ],
                ),
                logical_module("value", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7 0\n");
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
                &[
                    Member::new("Holder"),
                    Member::new("holder"),
                    Member::new("later"),
                ],
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
                logical_module("command", &[Member::new("Holder"), Member::new("holder")]),
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
fn local_constructor_reads_fresh_config_with_an_ordinary_method() {
    let fixture = run_fixture(
        FixtureOpts::new(
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
            vec![
                logical_module(
                    "command",
                    &[
                        Member::new("Command"),
                        Member::new("factory"),
                        Member::new("command"),
                    ],
                ),
                logical_module("value", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "true 0 false 7\n");
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
        let source = template.replace("CONFIG", config);
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
        let source = template
            .replace("CONSTRUCTOR_BODY", constructor_body)
            .replace("CONFIG", config);
        let colocated = run_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module_with_anon(
                    "all",
                    &[
                        Member::new("Command"),
                        Member::new("factory"),
                        Member::new("command"),
                        Member::new("later"),
                    ],
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
                        &[
                            Member::new("Command"),
                            Member::new("factory"),
                            Member::new("command"),
                        ],
                    ),
                    logical_module_with_anon("value", &[Member::new("later")], &["later = 7;"]),
                ],
            )
            .with_unassigned_mode(unassigned_mode_inline()),
            &["command", "value"],
            &[],
        );
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
                    &[
                        Member::new("Command"),
                        Member::new("factory"),
                        Member::new("isCommand"),
                        Member::new("command"),
                    ],
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
                    &[
                        Member::new("Checker"),
                        Member::new("Command"),
                        Member::new("create"),
                        Member::new("command"),
                    ],
                    &["globalThis.savedChecker = Checker;"],
                ),
                logical_module("value", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7\n");
}

#[test]
fn escaped_constructorless_class_allocation_does_not_reenter_global_callback() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const saved = () => later;
globalThis.saved = saved;
class Checker { isAllowed() { return true; } }
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
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7\n");
}

#[test]
fn escaped_class_work_at_construction_keeps_callback_reentry_possible() {
    for checker in [
        "class Checker { constructor() { globalThis.saved(); } }",
        "class Checker { result = globalThis.saved(); }",
    ] {
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &format!(
                    r#"const saved = () => later;
globalThis.saved = saved;
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
                ),
                vec![
                    logical_module("command", &[Member::new("command")]),
                    logical_module("value", &[Member::new("later")]),
                ],
            ),
            &["command", "value"],
            &[],
        );
    }
}

#[test]
fn reassigned_escaped_class_keeps_unknown_constructor_alternative() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
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
fn opaque_checker_receiving_config_keeps_callback_reentry_possible() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
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
fn constructor_prototype_getter_collision_remains_opaque() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"class Command {
  get config() { return 0; }
  constructor(config) { this.config = config; }
}
const command = new Command({ execute: () => later });
const later = 7;
export { Command, command, later };
"#,
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
fn negated_constructor_values_keep_getter_and_call_effects() {
    for value in ["!config.focus", "!config.execute()"] {
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &format!(
                    r#"class Command {{
  constructor(config) {{ this.focus = {value}; }}
}}
const command = new Command({{
  get focus() {{ return later; }},
  execute: () => later,
}});
const later = 7;
export {{ Command, command, later }};
"#
                ),
                vec![
                    logical_module("command", &[Member::new("command")]),
                    logical_module("value", &[Member::new("later")]),
                ],
            ),
            &["command", "value"],
            &[],
        );
    }
}

#[test]
fn constructor_that_invokes_callback_still_constrains_order() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"class Command {
  constructor(config) { config.execute(); }
}
function factory(config) { return new Command(config); }
const command = factory({ execute: () => later });
const later = 7;
export { Command, factory, command, later };
"#,
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
fn constructor_setter_remains_opaque() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"class Command {
  set config(value) { value.execute(); }
  constructor(config) { this.config = config; }
}
function factory(config) { return new Command(config); }
const command = factory({ execute: () => later });
const later = 7;
export { Command, factory, command, later };
"#,
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
fn constructor_config_getter_still_constrains_order() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"class Command {
  constructor(config) { this.weight = config.weight ?? 0; }
}
const command = new Command({ get weight() { return later; } });
const later = 7;
export { Command, command, later };
"#,
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
fn derived_constructor_remains_opaque() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"class Base {}
class Command extends Base {
  constructor(config) { super(); this.config = config; }
}
function factory(config) { return new Command(config); }
const command = factory({ execute: () => later });
const later = 7;
export { Base, Command, factory, command, later };
"#,
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
fn instance_field_initializer_runs_when_constructed() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const later = 7;
function execute() { return later; }
class Command { result = execute(); }
const command = new Command();
console.log(command.result);
export { Command, execute, command, later };
"#,
        vec![
            logical_module("command", &[Member::new("command")]),
            logical_module("value", &[Member::new("later")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "7\n");
}

#[test]
fn instance_field_call_before_later_binding_constrains_order() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"class Command { result = execute(); }
function execute() { return later; }
const command = new Command();
const later = 7;
export { Command, execute, command, later };
"#,
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
                logical_module("command", &[Member::new("Command"), Member::new("command")]),
                logical_module("value", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7\n");
}

#[test]
fn constructor_that_returns_another_object_remains_opaque() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"class Command {
  constructor(config) { return { execute: config.execute }; }
}
function factory(config) { return new Command(config); }
const command = factory({ execute: () => later });
const later = 7;
export { Command, factory, command, later };
"#,
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
fn published_instance_can_expose_stored_callback() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"class Command { constructor(config) { this.config = config; } }
function factory(config) { return new Command(config); }
const command = factory({ execute: () => later });
globalThis.saved = command;
console.log("ready");
const later = 7;
export { Command, factory, command, later };
"#,
            vec![
                logical_module("command", &[Member::new("command")]),
                logical_module("value", &[Member::new("later")]),
            ],
        ),
        &["anon_residual_sentinel", "value"],
        &[],
    );
}

#[test]
fn returned_closure_is_not_invoked_by_factory() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const value = 1;
function factory(callback) {
  callback(0);
  return () => value;
}
const mounted = factory(x => x);
console.log(mounted());
export { value, factory, mounted };
"#,
            vec![logical_module("mount", &[Member::new("mounted")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "1\n");
}

#[test]
fn returned_closure_invoked_during_initialization_constrains_order() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const value = 1;
function factory(callback) {
  callback(0);
  return () => value;
}
const mounted = factory(x => x)();
console.log(mounted);
export { value, factory, mounted };
"#,
            vec![logical_module("mount", &[Member::new("mounted")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["mount", "anon_residual_sentinel"],
    );
}

#[test]
fn omitted_callback_uses_default_without_invoking_returned_closure() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const value = 1;
function factory(callback = x => x) {
  callback(0);
  return () => value;
}
const mounted = factory();
console.log(mounted());
export { value, factory, mounted };
"#,
            vec![logical_module("mount", &[Member::new("mounted")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "1\n");
}

#[test]
fn callbacks_from_separate_calls_do_not_cross_contaminate() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const value = 1;
function factory(callback) {
  callback();
  return () => value;
}
const safe = factory(() => 0);
const other = factory(() => value);
console.log(safe(), other());
export { value, factory, safe, other };
"#,
            vec![logical_module("mount", &[Member::new("safe")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "1 1\n");
}

#[test]
fn returned_closure_keeps_its_call_site_callback() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const value = 1;
function factory(callback) { return () => callback(); }
const safe = factory(() => 0);
const other = factory(() => value);
const mounted = safe();
console.log(mounted, other());
export { value, factory, safe, other, mounted };
"#,
            vec![logical_module(
                "mount",
                &[
                    Member::new("factory"),
                    Member::new("safe"),
                    Member::new("mounted"),
                ],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "0 1\n");
}

#[test]
fn returned_closure_reaches_a_top_level_alias() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const value = 1;
function factory(callback) { return () => callback(); }
const created = factory(() => 0);
const alias = created;
const mounted = alias();
const other = factory(() => value);
console.log(mounted);
export { value, factory, created, alias, mounted, other };
"#,
            vec![logical_module(
                "mount",
                &[
                    Member::new("factory"),
                    Member::new("created"),
                    Member::new("alias"),
                    Member::new("mounted"),
                ],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "0\n");
}

#[test]
fn read_after_conditional_await_may_still_run_at_init() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const value = 1;
async function factory(flag) {
  if (flag) await Promise.resolve();
  return value;
}
const mounted = factory(false);
mounted.then(console.log);
export { value, factory, mounted };
"#,
            vec![logical_module(
                "mount",
                &[Member::new("factory"), Member::new("mounted")],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["mount", "anon_residual_sentinel"],
    );
}

#[test]
fn catch_after_possible_await_may_run_at_init() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const value = 1;
async function factory(flag) {
  try { if (flag) throw 0; await 0; }
  catch { return value; }
}
const mounted = factory(true);
mounted.then(console.log);
export { value, factory, mounted };
"#,
            vec![logical_module(
                "mount",
                &[Member::new("factory"), Member::new("mounted")],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["mount", "anon_residual_sentinel"],
    );
}

#[test]
fn later_switch_case_may_run_before_earlier_case_await() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const value = 1;
async function factory(choice) {
  switch (choice) {
    case 0: await 0; break;
    default: return value;
  }
}
const mounted = factory(1);
mounted.then(console.log);
export { value, factory, mounted };
"#,
            vec![logical_module(
                "mount",
                &[Member::new("factory"), Member::new("mounted")],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["mount", "anon_residual_sentinel"],
    );
}

#[test]
fn loop_body_may_run_before_await_in_update() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const value = 1;
async function factory() {
  for (let i = 0; i < 1; await 0) return value;
}
const mounted = factory();
mounted.then(console.log);
export { value, factory, mounted };
"#,
            vec![logical_module(
                "mount",
                &[Member::new("factory"), Member::new("mounted")],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["mount", "anon_residual_sentinel"],
    );
}

#[test]
fn factory_with_plain_intrinsics_defers_its_wrapper_body() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const value = 1;
function factory(callback, decorate = x => x) {
  const cache = new WeakMap();
  function wrapper() { return callback(); }
  return Object.assign(wrapper, { reader: decorate(callback) });
}
const mounted = factory(() => value);
console.log(mounted());
export { value, factory, mounted };
"#,
            vec![logical_module("mount", &[Member::new("mounted")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "1\n");
}

#[test]
fn fresh_function_data_property_write_keeps_returned_body_deferred() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const value = 1;
function factory(callback) {
  function wrapper() { return callback(); }
  wrapper.label = "wrapper";
  return Object.assign(wrapper, { reader: () => 0 });
}
const mounted = factory(() => value);
console.log(mounted());
export { value, factory, mounted };
"#,
            vec![logical_module(
                "mount",
                &[
                    Member::new("factory").with_purity(MemberPurity::Pure),
                    Member::new("mounted"),
                ],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "1\n");
}

#[test]
fn object_assign_getter_still_constrains_order() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const value = 1;
function factory() {
  function wrapper() { return 0; }
  return Object.assign(wrapper, { get reader() { return value; } });
}
const mounted = factory();
console.log(mounted());
export { value, factory, mounted };
"#,
            vec![logical_module(
                "mount",
                &[Member::new("factory"), Member::new("mounted")],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["mount", "anon_residual_sentinel"],
    );
}

#[test]
fn reassigned_object_assign_target_does_not_count_as_fresh() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const value = 1;
function factory() {
  function wrapper() {}
  wrapper = { set item(next) { console.log(value, next); } };
  return Object.assign(wrapper, { item: 0 });
}
const mounted = factory();
console.log(mounted);
export { value, factory, mounted };
"#,
            vec![logical_module(
                "mount",
                &[Member::new("factory"), Member::new("mounted")],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["mount", "anon_residual_sentinel"],
    );
}

#[test]
fn property_write_can_install_a_setter_before_object_assign() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const value = 1;
function factory() {
  function wrapper() {}
  wrapper.__proto__ = { set item(next) { globalThis.seen = value + next; } };
  return Object.assign(wrapper, { item: 0 });
}
const mounted = factory();
console.log(globalThis.seen);
export { value, factory, mounted };
"#,
            vec![logical_module(
                "mount",
                &[Member::new("factory"), Member::new("mounted")],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["mount", "anon_residual_sentinel"],
    );
}

#[test]
fn called_helper_can_install_a_setter_before_object_assign() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const value = 1;
function install(wrapper) {
  wrapper.__proto__ = { set item(next) { globalThis.seen = value + next; } };
}
function factory() {
  function wrapper() {}
  install(wrapper);
  return Object.assign(wrapper, { item: 0 });
}
const mounted = factory();
console.log(globalThis.seen);
export { value, install, factory, mounted };
"#,
            vec![logical_module(
                "mount",
                &[
                    Member::new("install"),
                    Member::new("factory"),
                    Member::new("mounted"),
                ],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["mount", "anon_residual_sentinel"],
    );
}

#[test]
fn object_assign_source_can_replace_target_prototype() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const value = 1;
function factory() {
  function wrapper() {}
  Object.assign(wrapper, { ["__proto__"]: { set item(next) { globalThis.seen = value + next; } } });
  return Object.assign(wrapper, { item: 0 });
}
const mounted = factory();
console.log(globalThis.seen);
export { value, factory, mounted };
"#,
            vec![logical_module(
                "mount",
                &[Member::new("factory"), Member::new("mounted")],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["mount", "anon_residual_sentinel"],
    );
}

#[test]
fn top_level_object_assign_can_call_a_setter_from_an_earlier_statement() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const value = 1;
function wrapper() {}
wrapper.__proto__ = { set item(next) { globalThis.seen = value + next; } };
const mounted = Object.assign(wrapper, { item: 0 });
console.log(globalThis.seen);
export { value, wrapper, mounted };
"#,
            vec![logical_module_with_anon(
                "mount",
                &[Member::new("wrapper"), Member::new("mounted")],
                &["wrapper.__proto__ = { set item(next) { globalThis.seen = value + next; } };"],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["mount", "anon_residual_sentinel"],
    );
}

#[test]
fn direct_property_write_can_call_a_local_setter() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const value = 1;
function factory() {
  function wrapper() {}
  wrapper.__proto__ = { set item(next) { globalThis.seen = value + next; } };
  wrapper.item = 0;
  return wrapper;
}
const mounted = factory();
console.log(globalThis.seen);
export { value, factory, mounted };
"#,
            vec![logical_module(
                "mount",
                &[Member::new("factory"), Member::new("mounted")],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["mount", "anon_residual_sentinel"],
    );
}

#[test]
fn top_level_property_write_can_call_a_setter_from_an_earlier_statement() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const value = 1;
function wrapper() {}
wrapper.__proto__ = { set item(next) { globalThis.seen = value + next; } };
wrapper.item = 0;
console.log(globalThis.seen);
export { value, wrapper };
"#,
            vec![logical_module_with_anon(
                "mount",
                &[Member::new("wrapper")],
                &[
                    "wrapper.__proto__ = { set item(next) { globalThis.seen = value + next; } };",
                    "wrapper.item = 0;",
                ],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["mount", "anon_residual_sentinel"],
    );
}

#[test]
fn spread_argument_can_run_an_iterator_body() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const value = 1;
function* args() { yield value; }
function factory(item) { return item; }
const mounted = factory(...args());
console.log(mounted);
export { value, args, factory, mounted };
"#,
            vec![logical_module(
                "mount",
                &[
                    Member::new("args"),
                    Member::new("factory"),
                    Member::new("mounted"),
                ],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["mount", "anon_residual_sentinel"],
    );
}

#[test]
fn generator_parameter_default_runs_at_call_time() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const value = 1;
function read() { return value; }
function* items(item = read()) { yield item; }
const mounted = items();
console.log(mounted);
export { value, read, items, mounted };
"#,
            vec![logical_module(
                "mount",
                &[Member::new("items"), Member::new("mounted")],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["mount", "anon_residual_sentinel"],
    );
}

#[test]
fn function_passed_through_parameter_reads_initialized_value() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const a = 1;
const b = a + 1;
function foo() { return b; }
function bar(callback) { return callback(); }
const result = bar(foo);
console.log(result);
export { a, b, foo, bar, result };
"#,
            vec![
                logical_module(
                    "mod_a",
                    &[
                        Member::new("a"),
                        Member::new("foo"),
                        Member::new("bar"),
                        Member::new("result"),
                    ],
                ),
                logical_module("mod_b", &[Member::new("b")]),
            ],
        ),
        &["mod_a", "mod_b"],
    );
}

#[test]
fn uninstantiated_constructor_does_not_create_an_eager_cycle() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"class Foo { constructor() { bar(); } }
function bar() { return b; }
const b = Foo.name;
console.log(b);
export { Foo, bar, b };
"#,
        vec![
            logical_module("mod_a", &[Member::new("Foo"), Member::new("bar")]),
            logical_module("mod_b", &[Member::new("b")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "Foo\n");
}

#[test]
fn uninstantiated_instance_field_does_not_create_an_eager_cycle() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"class Foo { value = bar(); }
function bar() { return b; }
const b = Foo.name;
console.log(b);
export { Foo, bar, b };
"#,
        vec![
            logical_module("mod_a", &[Member::new("Foo"), Member::new("bar")]),
            logical_module("mod_b", &[Member::new("b")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "Foo\n");
}

#[test]
fn static_field_call_still_runs_during_class_definition() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const b = 3;
function bar() { return b; }
class Foo { static value = bar(); }
console.log(Foo.value);
export { b, bar, Foo };
"#,
        vec![
            logical_module("mod_a", &[Member::new("bar"), Member::new("Foo")]),
            logical_module("mod_b", &[Member::new("b")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "3\n");
}

#[test]
fn computed_method_key_still_runs_during_class_definition() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const b = "value";
function bar() { return b; }
class Foo { [bar()]() {} }
console.log(typeof Foo.prototype.value);
export { b, bar, Foo };
"#,
        vec![
            logical_module("mod_a", &[Member::new("bar"), Member::new("Foo")]),
            logical_module("mod_b", &[Member::new("b")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "function\n");
}

#[test]
fn constructor_body_reads_initialized_value() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const a = 1;
const b = a + 1;
class Foo { constructor() { this.value = b; } }
const result = new Foo().value;
console.log(result);
export { a, b, Foo, result };
"#,
            vec![
                logical_module(
                    "mod_a",
                    &[Member::new("a"), Member::new("Foo"), Member::new("result")],
                ),
                logical_module("mod_b", &[Member::new("b")]),
            ],
        ),
        &["mod_a", "mod_b"],
    );
}

#[test]
fn callback_partition_with_initialized_value_in_dependency_runs() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = 1;
const b = a + 1;
function foo() { return b; }
function bar(callback) { return callback(); }
const result = bar(foo);
console.log(result);
export { a, b, foo, bar, result };
"#,
        vec![
            logical_module(
                "mod_a",
                &[
                    Member::new("foo"),
                    Member::new("bar"),
                    Member::new("result"),
                ],
            ),
            logical_module("mod_b", &[Member::new("a"), Member::new("b")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "2\n");
}

#[test]
fn constructor_partition_with_initialized_value_in_dependency_runs() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = 1;
const b = a + 1;
class Foo { constructor() { this.value = b; } }
const result = new Foo().value;
console.log(result);
export { a, b, Foo, result };
"#,
        vec![
            logical_module("mod_a", &[Member::new("Foo"), Member::new("result")]),
            logical_module("mod_b", &[Member::new("a"), Member::new("b")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "2\n");
}

#[test]
fn callback_returned_through_parameter_reads_initialized_value() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const a = 1;
const b = a + 1;
function foo() { return b; }
function identity(callback) { return callback; }
const result = identity(foo)();
console.log(result);
export { a, b, foo, identity, result };
"#,
            vec![
                logical_module(
                    "mod_a",
                    &[
                        Member::new("a"),
                        Member::new("foo"),
                        Member::new("identity"),
                        Member::new("result"),
                    ],
                ),
                logical_module("mod_b", &[Member::new("b")]),
            ],
        ),
        &["mod_a", "mod_b"],
    );
}

#[test]
fn callback_held_in_static_object_property_reads_initialized_value() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const a = 1;
const b = a + 1;
function foo() { return b; }
const box = { invoke: foo };
const result = box.invoke();
console.log(result);
export { a, b, foo, box, result };
"#,
            vec![
                logical_module(
                    "mod_a",
                    &[
                        Member::new("a"),
                        Member::new("foo"),
                        Member::new("box"),
                        Member::new("result"),
                    ],
                ),
                logical_module("mod_b", &[Member::new("b")]),
            ],
        ),
        &["mod_a", "mod_b"],
    );
}

#[test]
fn return_call_chain_through_parameters_recursion_and_property_fanout_preserves_output() {
    const RETURN_DEPTH: usize = 64;
    const PROPERTY_FANOUT: usize = 24;

    let mut source = String::from("function base() { return 7; }\n");
    for index in 0..RETURN_DEPTH {
        writeln!(source, "function f{index}() {{ return f{}(); }}", index + 1).unwrap();
    }
    writeln!(source, "function f{RETURN_DEPTH}() {{ return base; }}").unwrap();
    source.push_str(
        "function apply(callback, depth) {\n\
           if (depth > 0) return apply(callback, depth - 1);\n\
           return callback();\n\
         }\n\
         const source = { run: f0 };\n",
    );
    for index in 0..PROPERTY_FANOUT {
        writeln!(source, "const branch{index} = {{ run: source.run }};").unwrap();
    }
    writeln!(
        source,
        "const result = apply(branch{}.run, 2)();",
        PROPERTY_FANOUT - 1
    )
    .unwrap();
    source.push_str("console.log(result);\nexport { result };\n");

    let fixture = run_fixture(
        FixtureOpts::new(&source, vec![]).with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7\n");
}

#[test]
fn future_uncreated_command_is_not_an_available_setter_for_fresh_property_write() {
    let source = r#"function dormant() { return command; }
const reference = dormant;
globalThis.hostCall = () => {};
globalThis.hostCall();
const box = {};
box.value = 1;
const command = { run: () => later };
const later = 7;
console.log(box.value, later);
export { box, later };
"#;
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "value/box",
                    &[Member::new("box"), Member::new("command")],
                    &[
                        "globalThis.hostCall = () => {};",
                        "globalThis.hostCall();",
                        "box.value = 1;",
                    ],
                ),
                logical_module("value/later", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "1 7\n");
}

fn expect_property_write_reentry_conflict(source: &str, setup: &[&str]) {
    let mut anonymous = setup.to_vec();
    anonymous.push("box.value = 1;");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon("value/box", &[Member::new("box")], &anonymous),
                logical_module("value/later", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["value/box", "value/later"],
        &[],
    );
}

#[test]
fn future_hoisted_setter_remains_available_before_its_declaration() {
    expect_property_write_reentry_conflict(
        r#"const box = {};
Object.defineProperty(box, "value", { set: setter, configurable: true });
box.value = 1;
function setter() { return later; }
const later = 7;
export { box, later };
"#,
        &["Object.defineProperty(box, \"value\", { set: setter, configurable: true });"],
    );
}

#[test]
fn direct_eval_installed_setter_keeps_whole_chunk_fallback() {
    let source = r#"function dormant() { return command; }
const box = {};
eval('Object.defineProperty(box, "value", { set() { return command.run(); }, configurable: true })');
box.value = 1;
const command = { run: () => later };
const later = 7;
export { box, command, later };
"#;
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "value/box",
                    &[Member::new("box"), Member::new("command")],
                    &[
                        "eval('Object.defineProperty(box, \"value\", { set() { return command.run(); }, configurable: true })');",
                        "box.value = 1;",
                    ],
                ),
                logical_module("value/later", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_admission_overrides(&["a1_eval"]),
        &["value/box", "value/later"],
        &[],
    );
}

#[test]
fn proxy_set_trap_still_reads_future_value() {
    expect_property_write_reentry_conflict(
        r#"const box = new Proxy({}, { set() { return later; } });
box.value = 1;
const later = 7;
export { box, later };
"#,
        &[],
    );
}

#[test]
fn assignment_before_future_var_declaration_can_install_setter() {
    expect_property_write_reentry_conflict(
        r#"const box = {};
setter = () => later;
Object.defineProperty(box, "value", { set: setter, configurable: true });
box.value = 1;
var setter;
const later = 7;
export { box, later };
"#,
        &[
            "setter = () => later;",
            "Object.defineProperty(box, \"value\", { set: setter, configurable: true });",
        ],
    );
}

#[test]
fn executed_callback_reading_future_const_still_blocks_split() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"globalThis.invoke = (callback) => callback();
const box = {};
globalThis.invoke(() => command);
box.value = 1;
const command = { run: () => 7 };
export { box, command };
"#,
            vec![
                logical_module_with_anon(
                    "value/box",
                    &[Member::new("box")],
                    &[
                        "globalThis.invoke = (callback) => callback();",
                        "globalThis.invoke(() => command);",
                        "box.value = 1;",
                    ],
                ),
                logical_module("value/command", &[Member::new("command")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["value/box", "value/command"],
        &[],
    );
}

#[test]
fn async_setter_read_before_first_await_still_blocks_split() {
    expect_property_write_reentry_conflict(
        r#"const box = {};
Object.defineProperty(box, "value", { set: async function setter() { const observed = later; await 0; return observed; } });
box.value = 1;
const later = 7;
export { box, later };
"#,
        &[
            "Object.defineProperty(box, \"value\", { set: async function setter() { const observed = later; await 0; return observed; } });",
        ],
    );
}

#[test]
fn async_setter_read_after_first_await_can_split() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const box = {};
Object.defineProperty(box, "value", { set: async function setter() { await 0; return later; } });
box.value = 1;
const later = 7;
console.log(later);
export { box, later };
"#,
            vec![
                logical_module_with_anon(
                    "value/box",
                    &[Member::new("box")],
                    &[
                        "Object.defineProperty(box, \"value\", { set: async function setter() { await 0; return later; } });",
                        "box.value = 1;",
                    ],
                ),
                logical_module("value/later", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7\n");
}

#[test]
fn nested_setter_created_before_await_still_blocks_split() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"const box = {};
async function install() {
  const setter = () => later;
  Object.defineProperty(box, "value", { set: setter });
  await 0;
}
install();
box.value = 1;
const later = 7;
export { box, later };
"#,
            vec![
                logical_module_with_anon(
                    "value/box",
                    &[Member::new("box"), Member::new("install")],
                    &["install();", "box.value = 1;"],
                ),
                logical_module("value/later", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["value/box", "value/later"],
        &[],
    );
}

#[test]
fn hoisted_nested_setter_declared_after_await_still_blocks_split() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"const box = {};
async function install() {
  Object.defineProperty(box, "value", { set: setter });
  await 0;
  function setter() { return later; }
}
install();
box.value = 1;
const later = 7;
export { box, later };
"#,
            vec![
                logical_module_with_anon(
                    "value/box",
                    &[Member::new("box"), Member::new("install")],
                    &["install();", "box.value = 1;"],
                ),
                logical_module("value/later", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["value/box", "value/later"],
        &[],
    );
}

#[test]
fn nested_setter_created_after_parent_await_can_split() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const box = {};
async function install() {
  await 0;
  const setter = () => later;
  Object.defineProperty(box, "value", { set: setter });
}
install();
globalThis.hostCall = () => {};
globalThis.hostCall();
box.value = 1;
const later = 7;
console.log(later);
export { box, later };
"#,
            vec![
                logical_module_with_anon(
                    "value/box",
                    &[Member::new("box"), Member::new("install")],
                    &[
                        "install();",
                        "globalThis.hostCall = () => {};",
                        "globalThis.hostCall();",
                        "box.value = 1;",
                    ],
                ),
                logical_module("value/later", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7\n");
}

const CLASS_MEMBER_PEER_CHUNKS: &[(&str, &str)] = &[(
    "static/peer",
    r#"export function inspect(Reader) {
  const instance = new Reader();
  void instance.read();
  return "ok";
}
"#,
)];

const CLASS_MEMBER_PRE_AWAIT_SOURCE: &str = r#"import { inspect } from "./peer.js";
let later = 0;
class Reader { async read() { globalThis.observed = later; await 0; return later; } }
const result = inspect(Reader);
later = 7;
console.log(result, later, globalThis.observed);
export { Reader, result, later };
"#;

fn opaque_class_member_opts(source: &'static str, assign_later: bool) -> FixtureOpts<'static> {
    let later = if assign_later {
        logical_module_with_anon("value/later", &[Member::new("later")], &["later = 7;"])
    } else {
        logical_module("value/later", &[Member::new("later")])
    };
    FixtureOpts::new(
        source,
        vec![
            logical_module(
                "value/reader",
                &[Member::new("Reader"), Member::new("result")],
            ),
            later,
        ],
    )
    .with_chunk_id("static/provider")
    .with_extra_chunks(CLASS_MEMBER_PEER_CHUNKS)
    .with_unassigned_mode(unassigned_mode_inline())
}

#[test]
fn opaque_class_member_skips_post_await_read() {
    let fixture = run_fixture(opaque_class_member_opts(
        r#"import { inspect } from "./peer.js";
class Reader { async read() { await 0; return later; } }
const result = inspect(Reader);
const later = 7;
console.log(result, later);
export { Reader, result, later };
"#,
        false,
    ));
    assert_preserved_entry_output(&fixture, "ok 7\n");
}

#[test]
fn opaque_class_member_keeps_pre_await_read() {
    expect_atomic_conflict_rejection(
        opaque_class_member_opts(CLASS_MEMBER_PRE_AWAIT_SOURCE, true),
        &["value/reader", "value/later"],
        &[],
    );
}

#[test]
fn opaque_class_member_pre_await_read_runs_when_colocated() {
    let fixture = run_fixture(
        FixtureOpts::new(
            CLASS_MEMBER_PRE_AWAIT_SOURCE,
            vec![logical_module_with_anon(
                "value/all",
                &[
                    Member::new("Reader"),
                    Member::new("result"),
                    Member::new("later"),
                ],
                &["later = 7;"],
            )],
        )
        .with_chunk_id("static/provider")
        .with_extra_chunks(CLASS_MEMBER_PEER_CHUNKS)
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "ok 7 0\n");
}

#[test]
fn opaque_class_member_keeps_instance_field_read() {
    expect_atomic_conflict_rejection(
        opaque_class_member_opts(
            r#"import { inspect } from "./peer.js";
let later = 0;
class Reader {
  value = (globalThis.observed = later);
  async read() { await 0; return this.value; }
}
const result = inspect(Reader);
later = 7;
console.log(result, later, globalThis.observed);
export { Reader, result, later };
"#,
            true,
        ),
        &["value/reader", "value/later"],
        &[],
    );
}

#[test]
fn opaque_class_member_keeps_getter_before_await() {
    expect_atomic_conflict_rejection(
        opaque_class_member_opts(
            r#"import { inspect } from "./peer.js";
let later = 0;
class Reader {
  get value() { return (globalThis.observed = later); }
  async read() { const value = this.value; await 0; return value; }
}
const result = inspect(Reader);
later = 7;
console.log(result, later, globalThis.observed);
export { Reader, result, later };
"#,
            true,
        ),
        &["value/reader", "value/later"],
        &[],
    );
}

#[test]
fn bounded_local_callback_fallback_ignores_post_await_read() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"async function load(count) {
  if (count > 0) return load(count - 1);
  await 0;
  return later;
}
const pending = load(100);
const later = 42;
console.log(later);
"#,
        vec![
            logical_module(
                "callbacks/pending",
                &[Member::new("load"), Member::new("pending")],
            ),
            logical_module("values/later", &[Member::new("later")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn bounded_local_callback_fallback_keeps_pre_await_read() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"let later = 0;
async function load(count) {
  globalThis.seen = later;
  if (count > 0) return load(count - 1);
  await 0;
}
const pending = load(100);
later = 42;
console.log(globalThis.seen, later);
"#,
            vec![
                logical_module(
                    "callbacks/pending",
                    &[Member::new("load"), Member::new("pending")],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        ),
        &["callbacks/pending", "values/later"],
        &[],
    );
}

#[test]
fn bounded_unknown_callback_fallback_ignores_post_await_read() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const anchor = {};
function bounce(n) { if (n) return bounce(n - 1); }
async function deferred() { await 0; return later; }
globalThis.registeredBounce = bounce;
globalThis.registeredDeferred = deferred;
globalThis.flush = () => {};
globalThis.flush();
const later = 7;
console.log(later);
export { anchor, bounce, deferred, later };
"#,
            vec![
                logical_module_with_anon(
                    "value/anchor",
                    &[
                        Member::new("anchor"),
                        Member::new("bounce"),
                        Member::new("deferred"),
                    ],
                    &[
                        "globalThis.registeredBounce = bounce;",
                        "globalThis.registeredDeferred = deferred;",
                        "globalThis.flush = () => {};",
                        "globalThis.flush();",
                    ],
                ),
                logical_module("value/later", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7\n");
}

#[test]
fn bounded_unknown_callback_fallback_keeps_pre_await_read() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"function bounce(n) { if (n) return bounce(n - 1); }
async function immediate() { const value = later; await 0; return value; }
globalThis.registeredBounce = bounce;
globalThis.registeredImmediate = immediate;
globalThis.flush = () => {};
globalThis.flush();
const later = 7;
export { bounce, immediate, later };
"#,
            vec![
                logical_module_with_anon(
                    "value/callbacks",
                    &[Member::new("bounce"), Member::new("immediate")],
                    &[
                        "globalThis.registeredBounce = bounce;",
                        "globalThis.registeredImmediate = immediate;",
                        "globalThis.flush = () => {};",
                        "globalThis.flush();",
                    ],
                ),
                logical_module("value/later", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["value/callbacks", "value/later"],
        &[],
    );
}

#[test]
fn bounded_unknown_callback_fallback_keeps_nested_callback_created_before_await() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"function bounce(n) { if (n) return bounce(n - 1); }
async function publish() {
  globalThis.registeredNested = () => later;
  await 0;
}
globalThis.registeredBounce = bounce;
publish();
globalThis.flush = () => {};
globalThis.flush();
const later = 7;
export { bounce, publish, later };
"#,
            vec![
                logical_module_with_anon(
                    "value/callbacks",
                    &[Member::new("bounce"), Member::new("publish")],
                    &[
                        "globalThis.registeredBounce = bounce;",
                        "publish();",
                        "globalThis.flush = () => {};",
                        "globalThis.flush();",
                    ],
                ),
                logical_module("value/later", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["value/callbacks", "value/later"],
        &[],
    );
}
