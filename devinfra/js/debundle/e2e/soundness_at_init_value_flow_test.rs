//! At-init calls must account for reads reached through function values.
//! Each input runs successfully as one ESM chunk. An accepted split must
//! preserve that result, even when a call crosses a parameter or `new`.

use debundle_e2e_support::*;

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
