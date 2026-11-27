//! Returned closures stay deferred unless a call, async read or accessor runs their body at init.

use super::{in_case, members};
use debundle_e2e_support::*;

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
