//! At-init calls must account for reads reached through function values.
//! Each input runs successfully as one ESM chunk. An accepted split must
//! preserve that result, even when a call crosses a parameter or `new`.

use debundle_e2e_support::*;
use std::fmt::Write as _;
use std::panic::{AssertUnwindSafe, catch_unwind, resume_unwind};

/// Run one case of a table, naming it when an assertion inside fails.
fn in_case(case: &str, check: impl FnOnce()) {
    if let Err(panic) = catch_unwind(AssertUnwindSafe(check)) {
        eprintln!("case {case:?} failed");
        resume_unwind(panic);
    }
}

fn members(names: &[&'static str]) -> Vec<Member> {
    names.iter().copied().map(Member::new).collect()
}

// ─── constructors receiving callbacks ───────────────────────────────────

fn command_later_split_modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module("command", &[Member::new("command")]),
        logical_module("value", &[Member::new("later")]),
    ]
}

/// `command` before `later`: accepted only if building `command` cannot run
/// a callback that reads `later`.
fn command_later_split(source: &str) -> FixtureOpts<'_> {
    FixtureOpts::new(source, command_later_split_modules())
        .with_import_order(&["command", "value", "residual"])
}

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
                    &binding_members(["saved", "Checker", "Command", "create", "command"]),
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
        in_case(checker, || {
            expect_atomic_conflict_rejection(
                command_later_split(&format!(
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
                )),
                &["command", "value"],
                &[],
            );
        });
    }
}

#[test]
fn negated_constructor_values_keep_getter_and_call_effects() {
    for value in ["!config.focus", "!config.execute()"] {
        in_case(value, || {
            expect_atomic_conflict_rejection(
                command_later_split(&format!(
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
                )),
                &["command", "value"],
                &[],
            );
        });
    }
}

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

/// Module `mount` holds `names` and the `anon` statements; the rest stays inline.
fn mount_split<'a>(source: &'a str, names: &[&'static str], anon: &[&str]) -> FixtureOpts<'a> {
    FixtureOpts::new(
        source,
        vec![logical_module_with_anon("mount", &members(names), anon)],
    )
    .with_unassigned_mode(unassigned_mode_inline())
}

/// A factory that returns (rather than calls) a closure leaves its body
/// for later; each closure keeps its own call site's callback.
#[test]
fn mount_split_keeps_returned_closures_deferred() {
    for (name, source, names, expected) in [
        (
            "returned closure is not invoked by the factory",
            r#"const value = 1;
function factory(callback) {
  callback(0);
  return () => value;
}
const mounted = factory(x => x);
console.log(mounted());
export { value, factory, mounted };
"#,
            &["mounted"][..],
            "1\n",
        ),
        (
            "omitted callback uses its default",
            r#"const value = 1;
function factory(callback = x => x) {
  callback(0);
  return () => value;
}
const mounted = factory();
console.log(mounted());
export { value, factory, mounted };
"#,
            &["mounted"],
            "1\n",
        ),
        (
            "callbacks from separate calls do not cross-contaminate",
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
            &["safe"],
            "1 1\n",
        ),
        (
            "returned closure keeps its call-site callback",
            r#"const value = 1;
function factory(callback) { return () => callback(); }
const safe = factory(() => 0);
const other = factory(() => value);
const mounted = safe();
console.log(mounted, other());
export { value, factory, safe, other, mounted };
"#,
            &["factory", "safe", "mounted"],
            "0 1\n",
        ),
        (
            "returned closure reaches a top-level alias",
            r#"const value = 1;
function factory(callback) { return () => callback(); }
const created = factory(() => 0);
const alias = created;
const mounted = alias();
const other = factory(() => value);
console.log(mounted);
export { value, factory, created, alias, mounted, other };
"#,
            &["factory", "created", "alias", "mounted"],
            "0\n",
        ),
        (
            "factory with plain intrinsics defers its wrapper body",
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
            &["mounted"],
            "1\n",
        ),
    ] {
        in_case(name, || {
            let fixture = run_fixture(mount_split(source, names, &[]));
            assert_preserved_entry_output(&fixture, expected);
        });
    }
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

#[track_caller]
fn expect_mount_cycle(source: &str, names: &[&'static str], anon: &[&str]) {
    expect_cycle_rejection(
        mount_split(source, names, anon),
        &["mount", "anon_residual_sentinel"],
    );
}

/// Calls that run a body (returned closure, iterator, parameter default)
/// during initialization still order `mount` after `value`.
#[test]
fn mount_split_rejects_calls_that_run_bodies_at_init() {
    for (name, source, names) in [
        (
            "returned closure invoked during initialization",
            r#"const value = 1;
function factory(callback) {
  callback(0);
  return () => value;
}
const mounted = factory(x => x)();
console.log(mounted);
export { value, factory, mounted };
"#,
            &["mounted"][..],
        ),
        (
            "spread argument runs an iterator body",
            r#"const value = 1;
function* args() { yield value; }
function factory(item) { return item; }
const mounted = factory(...args());
console.log(mounted);
export { value, args, factory, mounted };
"#,
            &["args", "factory", "mounted"],
        ),
        (
            "generator parameter default runs at call time",
            r#"const value = 1;
function read() { return value; }
function* items(item = read()) { yield item; }
const mounted = items();
console.log(mounted);
export { value, read, items, mounted };
"#,
            &["items", "mounted"],
        ),
    ] {
        in_case(name, || expect_mount_cycle(source, names, &[]));
    }
}

/// An async body runs synchronously up to its first `await`; reads on any
/// path that may precede it run at init.
#[test]
fn mount_split_rejects_async_reads_that_may_precede_suspension() {
    for (name, source) in [
        (
            "read after a conditional await",
            r#"const value = 1;
async function factory(flag) {
  if (flag) await Promise.resolve();
  return value;
}
const mounted = factory(false);
mounted.then(console.log);
export { value, factory, mounted };
"#,
        ),
        (
            "catch after a possible await",
            r#"const value = 1;
async function factory(flag) {
  try { if (flag) throw 0; await 0; }
  catch { return value; }
}
const mounted = factory(true);
mounted.then(console.log);
export { value, factory, mounted };
"#,
        ),
        (
            "later switch case before an earlier case's await",
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
        ),
        (
            "loop body before the await in its update",
            r#"const value = 1;
async function factory() {
  for (let i = 0; i < 1; await 0) return value;
}
const mounted = factory();
mounted.then(console.log);
export { value, factory, mounted };
"#,
        ),
    ] {
        in_case(name, || {
            expect_mount_cycle(source, &["factory", "mounted"], &[]);
        });
    }
}

/// `Object.assign` and property writes can run a getter or setter that
/// reads `value`, including one installed earlier on the target.
#[test]
fn mount_split_rejects_accessors_property_writes_may_run() {
    const PROTO_SETTER: &str =
        "wrapper.__proto__ = { set item(next) { globalThis.seen = value + next; } };";
    for (name, source, names, anon) in [
        (
            "Object.assign source getter",
            r#"const value = 1;
function factory() {
  function wrapper() { return 0; }
  return Object.assign(wrapper, { get reader() { return value; } });
}
const mounted = factory();
console.log(mounted());
export { value, factory, mounted };
"#,
            &["factory", "mounted"][..],
            &[][..],
        ),
        (
            "reassigned Object.assign target is not fresh",
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
            &["factory", "mounted"],
            &[],
        ),
        (
            "property write installs a setter before Object.assign",
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
            &["factory", "mounted"],
            &[],
        ),
        (
            "called helper installs a setter before Object.assign",
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
            &["install", "factory", "mounted"],
            &[],
        ),
        (
            "Object.assign source replaces the target prototype",
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
            &["factory", "mounted"],
            &[],
        ),
        (
            "top-level Object.assign calls a setter from an earlier statement",
            r#"const value = 1;
function wrapper() {}
wrapper.__proto__ = { set item(next) { globalThis.seen = value + next; } };
const mounted = Object.assign(wrapper, { item: 0 });
console.log(globalThis.seen);
export { value, wrapper, mounted };
"#,
            &["wrapper", "mounted"],
            &[PROTO_SETTER],
        ),
        (
            "direct property write calls a local setter",
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
            &["factory", "mounted"],
            &[],
        ),
        (
            "top-level property write calls a setter from an earlier statement",
            r#"const value = 1;
function wrapper() {}
wrapper.__proto__ = { set item(next) { globalThis.seen = value + next; } };
wrapper.item = 0;
console.log(globalThis.seen);
export { value, wrapper };
"#,
            &["wrapper"],
            &[PROTO_SETTER, "wrapper.item = 0;"],
        ),
    ] {
        in_case(name, || expect_mount_cycle(source, names, anon));
    }
}

// ─── function values reaching initialized bindings ──────────────────────

/// `mod_a` and `mod_b` with the given members, default unassigned mode.
fn ab_split<'a>(
    source: &'a str,
    mod_a: &[&'static str],
    mod_b: &[&'static str],
) -> FixtureOpts<'a> {
    FixtureOpts::new(
        source,
        vec![
            logical_module("mod_a", &members(mod_a)),
            logical_module("mod_b", &members(mod_b)),
        ],
    )
}

const CALLBACK_PARAMETER_SOURCE: &str = r#"const a = 1;
const b = a + 1;
function foo() { return b; }
function bar(callback) { return callback(); }
const result = bar(foo);
console.log(result);
export { a, b, foo, bar, result };
"#;

const CONSTRUCTOR_BODY_SOURCE: &str = r#"const a = 1;
const b = a + 1;
class Foo { constructor() { this.value = b; } }
const result = new Foo().value;
console.log(result);
export { a, b, Foo, result };
"#;

/// `result` calls into a read of `b`, which needs `a` from `mod_a`: a cycle
/// whichever way a function value reaches the call.
#[test]
fn function_values_reading_initialized_value_reject_split() {
    for (name, source, mod_a) in [
        (
            "function passed through a parameter",
            CALLBACK_PARAMETER_SOURCE,
            &["a", "foo", "bar", "result"][..],
        ),
        (
            "constructor body",
            CONSTRUCTOR_BODY_SOURCE,
            &["a", "Foo", "result"],
        ),
        (
            "callback returned through a parameter",
            r#"const a = 1;
const b = a + 1;
function foo() { return b; }
function identity(callback) { return callback; }
const result = identity(foo)();
console.log(result);
export { a, b, foo, identity, result };
"#,
            &["a", "foo", "identity", "result"],
        ),
        (
            "callback held in a static object property",
            r#"const a = 1;
const b = a + 1;
function foo() { return b; }
const box = { invoke: foo };
const result = box.invoke();
console.log(result);
export { a, b, foo, box, result };
"#,
            &["a", "foo", "box", "result"],
        ),
    ] {
        in_case(name, || {
            expect_cycle_rejection(ab_split(source, mod_a, &["b"]), &["mod_a", "mod_b"]);
        });
    }
}

/// Anchors for the rejections above: moving `a` with `b` breaks the cycle.
#[test]
fn function_values_reading_initialized_value_run_when_dependency_moves_with_it() {
    for (name, source, mod_a) in [
        (
            "callback assignment",
            CALLBACK_PARAMETER_SOURCE,
            &["foo", "bar", "result"][..],
        ),
        (
            "constructor assignment",
            CONSTRUCTOR_BODY_SOURCE,
            &["Foo", "result"],
        ),
    ] {
        in_case(name, || {
            let fixture = run_fixture(ab_split(source, mod_a, &["a", "b"]));
            assert_preserved_entry_output(&fixture, "2\n");
        });
    }
}

#[test]
fn uninstantiated_class_bodies_do_not_create_an_eager_cycle() {
    for (name, source) in [
        (
            "constructor",
            r#"class Foo { constructor() { bar(); } }
function bar() { return b; }
const b = Foo.name;
console.log(b);
export { Foo, bar, b };
"#,
        ),
        (
            "instance field",
            r#"class Foo { value = bar(); }
function bar() { return b; }
const b = Foo.name;
console.log(b);
export { Foo, bar, b };
"#,
        ),
    ] {
        in_case(name, || {
            let fixture = run_fixture(
                ab_split(source, &["Foo", "bar"], &["b"]).with_import_order(&["mod_b", "mod_a"]),
            );
            assert_preserved_entry_output(&fixture, "Foo\n");
        });
    }
}

#[test]
fn class_definition_time_calls_still_run() {
    for (name, source, expected) in [
        (
            "static field",
            r#"const b = 3;
function bar() { return b; }
class Foo { static value = bar(); }
console.log(Foo.value);
export { b, bar, Foo };
"#,
            "3\n",
        ),
        (
            "computed method key",
            r#"const b = "value";
function bar() { return b; }
class Foo { [bar()]() {} }
console.log(typeof Foo.prototype.value);
export { b, bar, Foo };
"#,
            "function\n",
        ),
    ] {
        in_case(name, || {
            let fixture = run_fixture(ab_split(source, &["bar", "Foo"], &["b"]));
            assert_preserved_entry_output(&fixture, expected);
        });
    }
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

// ─── property writes and setters ────────────────────────────────────────

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
                    &binding_members(["box", "command"]),
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

/// `box.value = 1` may run a setter reading `later`, so `value/box` (with the
/// `setup` statements) cannot precede `value/later`.
#[test]
fn property_write_rejects_reachable_setters() {
    for (name, source, setup) in [
        (
            "hoisted setter before its declaration",
            r#"const box = {};
Object.defineProperty(box, "value", { set: setter, configurable: true });
box.value = 1;
function setter() { return later; }
const later = 7;
export { box, later };
"#,
            &["Object.defineProperty(box, \"value\", { set: setter, configurable: true });"][..],
        ),
        (
            "Proxy set trap",
            r#"const box = new Proxy({}, { set() { return later; } });
box.value = 1;
const later = 7;
export { box, later };
"#,
            &[],
        ),
        (
            "assignment before a future var declaration",
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
        ),
        (
            "async setter reading before its first await",
            r#"const box = {};
Object.defineProperty(box, "value", { set: async function setter() { const observed = later; await 0; return observed; } });
box.value = 1;
const later = 7;
export { box, later };
"#,
            &[
                "Object.defineProperty(box, \"value\", { set: async function setter() { const observed = later; await 0; return observed; } });",
            ],
        ),
    ] {
        in_case(name, || {
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
        });
    }
}

/// Anchor for the pre-await async setter: a read after the first await is deferred.
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
                    &binding_members(["box", "command"]),
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

/// `install()` defines the setter synchronously before its first `await`,
/// whether the setter is created there or hoisted from after it.
#[test]
fn nested_setter_installed_before_await_still_blocks_split() {
    for (name, source) in [
        (
            "setter created before the await",
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
        ),
        (
            "hoisted setter declared after the await",
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
        ),
    ] {
        in_case(name, || {
            expect_atomic_conflict_rejection(
                FixtureOpts::new(
                    source,
                    vec![
                        logical_module_with_anon(
                            "value/box",
                            &binding_members(["box", "install"]),
                            &["install();", "box.value = 1;"],
                        ),
                        logical_module("value/later", &[Member::new("later")]),
                    ],
                )
                .with_unassigned_mode(unassigned_mode_inline()),
                &["value/box", "value/later"],
                &[],
            );
        });
    }
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
                    &binding_members(["box", "install"]),
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

// ─── class members handed to an opaque peer ─────────────────────────────

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
            logical_module("value/reader", &binding_members(["Reader", "result"])),
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

/// The peer may construct `Reader` and call `read`: reads that run before
/// its first `await` (in the method, a field or a getter) stay ordered.
#[test]
fn opaque_class_member_keeps_reads_before_await() {
    for (name, source) in [
        ("method body", CLASS_MEMBER_PRE_AWAIT_SOURCE),
        (
            "instance field",
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
        ),
        (
            "getter read before the await",
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
        ),
    ] {
        in_case(name, || {
            expect_atomic_conflict_rejection(
                opaque_class_member_opts(source, true),
                &["value/reader", "value/later"],
                &[],
            );
        });
    }
}

#[test]
fn opaque_class_member_pre_await_read_runs_when_colocated() {
    let fixture = run_fixture(
        FixtureOpts::new(
            CLASS_MEMBER_PRE_AWAIT_SOURCE,
            vec![logical_module_with_anon(
                "value/all",
                &binding_members(["Reader", "result", "later"]),
                &["later = 7;"],
            )],
        )
        .with_chunk_id("static/provider")
        .with_extra_chunks(CLASS_MEMBER_PEER_CHUNKS)
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "ok 7 0\n");
}

// ─── bounded callback fallback ──────────────────────────────────────────

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
            logical_module("callbacks/pending", &binding_members(["load", "pending"])),
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
                logical_module("callbacks/pending", &binding_members(["load", "pending"])),
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
                    &binding_members(["anchor", "bounce", "deferred"]),
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

/// Anchors for the post-await case above: the host may run a registered
/// callback, and its pre-await read (or a callback it creates before the
/// await) can observe `later`.
#[test]
fn bounded_unknown_callback_fallback_keeps_pre_await_effects() {
    for (name, source, names, anon) in [
        (
            "read before the await",
            r#"function bounce(n) { if (n) return bounce(n - 1); }
async function immediate() { const value = later; await 0; return value; }
globalThis.registeredBounce = bounce;
globalThis.registeredImmediate = immediate;
globalThis.flush = () => {};
globalThis.flush();
const later = 7;
export { bounce, immediate, later };
"#,
            &["bounce", "immediate"][..],
            &[
                "globalThis.registeredBounce = bounce;",
                "globalThis.registeredImmediate = immediate;",
                "globalThis.flush = () => {};",
                "globalThis.flush();",
            ][..],
        ),
        (
            "nested callback created before the await",
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
            &["bounce", "publish"],
            &[
                "globalThis.registeredBounce = bounce;",
                "publish();",
                "globalThis.flush = () => {};",
                "globalThis.flush();",
            ],
        ),
    ] {
        in_case(name, || {
            expect_atomic_conflict_rejection(
                FixtureOpts::new(
                    source,
                    vec![
                        logical_module_with_anon("value/callbacks", &members(names), anon),
                        logical_module("value/later", &[Member::new("later")]),
                    ],
                )
                .with_unassigned_mode(unassigned_mode_inline()),
                &["value/callbacks", "value/later"],
                &[],
            );
        });
    }
}

#[test]
fn bounded_callback_fallback_excludes_post_await_binding_write() {
    let source = bounded_callback_write_source("await 0; state = 1;");
    let fixture = run_fixture_with_args(
        bounded_callback_write_split(&source),
        &["--effect-conflicts-jsonl"],
    );
    assert_preserved_entry_output(&fixture, "326 0\n");
    let rows = read_jsonl(
        &fixture
            .report_root
            .join(&fixture.chunk_id)
            .join("effect_conflicts.jsonl"),
    );
    assert!(
        !rows.is_empty(),
        "the emitted order must exercise effect relocation"
    );
    assert!(
        rows.iter().all(|row| {
            row["cells"]
                .as_array()
                .unwrap()
                .iter()
                .all(|cell| cell["cell"]["kind"] != "binding" || cell["cell"]["binding"] != "state")
        }),
        "deferred binding writes must not become modeled initialization conflicts: {rows:#?}"
    );
}

fn bounded_callback_write_source(body: &str) -> String {
    let padding = vec!["() => 3"; 64].join(", ");
    format!(
        "async function deferred() {{ {body} }}\n\
         const result = JSON.stringify([deferred, {padding}]);\n\
         var state = 0;\n\
         console.log(result.length, state);\n"
    )
}

fn bounded_callback_write_split(source: &str) -> FixtureOpts<'_> {
    FixtureOpts::new(
        source,
        vec![
            logical_module("value/state", &binding_members(["deferred", "state"])),
            logical_module("callbacks/result", &[Member::new("result")]),
        ],
    )
}

#[test]
fn bounded_callback_fallback_retains_synchronous_and_uncovered_writes() {
    for body in [
        "state = 1; await 0;",
        "state++; await 0;",
        "({ value: state } = { value: 1 }); await 0;",
        "[state] = [1]; await 0;",
        "for (state of [1]) {} await 0;",
        "for (state in { key: 1 }) {} await 0;",
        "await (state = 1);",
        "if (globalThis.suspend) await 0; state = 1;",
        "try { await 0; } catch {} state = 1;",
        "globalThis.saved = () => { state = 1; }; await 0;",
        "globalThis.saved = write; await 0; function write() { state = 1; }",
        "state = await 0;",
        "[(state)] = [1]; await 0;",
        "await 0; [(state)] = [1];",
    ] {
        in_case(body, || {
            let source = bounded_callback_write_source(body);
            expect_atomic_conflict_rejection(
                bounded_callback_write_split(&source),
                &["value/state", "callbacks/result"],
                &[],
            );
        });
    }
}

#[test]
fn bounded_callback_fallback_keeps_deferred_writes_runnable() {
    for body in [
        "await 0; state = 1;",
        "await 0; state++;",
        "await 0; ({ value: state } = { value: 1 });",
        "await 0; [state] = [1];",
        "let value = await Promise.resolve(1); if (!value) return; state = value;",
        "await 0; const write = () => { state = 1; }; write();",
    ] {
        in_case(body, || {
            let source = format!(
                "{}deferred().then(() => console.log(state));\n",
                bounded_callback_write_source(body)
            );
            let fixture = run_fixture(bounded_callback_write_split(&source));
            assert_preserved_entry_output(&fixture, "326 0\n1\n");
        });
    }
}

#[test]
fn bounded_callback_fallback_keeps_deferred_writer_colocated() {
    let source = bounded_callback_write_source("await 0; state = 1;");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &source,
            vec![
                logical_module("callbacks/writer", &[Member::new("deferred")]),
                logical_module("value/state", &[Member::new("state")]),
            ],
        )
        .with_import_order(&["callbacks/writer", "value/state", "residual"]),
        &["callbacks/writer", "value/state"],
        &["deferred_rebind"],
    );
}
