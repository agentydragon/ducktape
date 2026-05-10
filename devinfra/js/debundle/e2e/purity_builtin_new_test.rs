//! Pin `new BuiltinContainer()` purity.
//! `new Map()`/`Set()`/`WeakMap()`/`WeakSet()`/`Array()` allocate
//! the fresh container and return — no iterator protocol, no
//! user-code getters. Same admission contract as
//! `PURE_GLOBAL_CALLS_WITH_PRIMITIVE_ARGS` / `PURE_GLOBAL_CALLS`.
//! Also covers the array-literal-iterable form for `Set`/`Map`:
//! built-in Array iterator + primitive-key Set.add/Map.set fire
//! no user code, so an Array-literal arg with all-Pure
//! elements (no spreads) is also pure.

use debundle_e2e_support::*;

// Cycle-forcing fixture: `b = new <Container>();` peeled to
// b_module while previous-SE neighbor `a` (IIFE call) and a
// downstream reader of `b` stay in residual. Without the rule
// `new …` is Unknown → b is SE → b → a s-edge crosses into
// residual; combined with residual → b_module (c reads b at
// init), spec is unrealizable. With the rule `new <Container>()`
// is Pure → b drops out of the S-chain.

#[test]
fn new_map() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = (() => 1)();
const b = new Map();
const c = b.size + a;
console.log(c);
export { a, b, c };
"#,
        vec![logical_module("b_module", &[Member::new("b")])],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/b_module.js",
        &["const b = new Map()"],
        &["const a"],
    );
    assert_entry_output(&fixture, "1\n");
}

#[test]
fn new_set() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = (() => 1)();
const b = new Set();
const c = b.size + a;
console.log(c);
export { a, b, c };
"#,
        vec![logical_module("b_module", &[Member::new("b")])],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/b_module.js",
        &["const b = new Set()"],
        &["const a"],
    );
    assert_entry_output(&fixture, "1\n");
}

#[test]
fn new_weakmap() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = (() => 1)();
const b = new WeakMap();
const c = (b ? "y" : "n") + a;
console.log(c);
export { a, b, c };
"#,
        vec![logical_module("b_module", &[Member::new("b")])],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/b_module.js",
        &["const b = new WeakMap()"],
        &["const a"],
    );
    assert_entry_output(&fixture, "y1\n");
}

#[test]
fn new_array() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = (() => 1)();
const b = new Array();
const c = b.length + a;
console.log(c);
export { a, b, c };
"#,
        vec![logical_module("b_module", &[Member::new("b")])],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/b_module.js",
        &["const b = new Array()"],
        &["const a"],
    );
    assert_entry_output(&fixture, "1\n");
}

#[test]
fn new_set_with_array_of_primitives() {
    // `new Set([prim_lit, prim_lit, ...])` — Array literal with
    // all-Pure elements. ECMA-262 §24.2.1.1: iterates via the
    // built-in Array iterator and calls `Set.add` per element;
    // no user code on primitive keys.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = (() => 1)();
const b = new Set(["x", "y", "z"]);
const c = b.size + a;
console.log(c);
export { a, b, c };
"#,
        vec![logical_module("b_module", &[Member::new("b")])],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/b_module.js",
        &["const b = new Set("],
        &["const a"],
    );
    assert_entry_output(&fixture, "4\n");
}

#[test]
fn new_map_with_array_of_pure_pairs() {
    // `new Map([[k, v], [k, v], ...])` — Array of 2-tuple Array
    // literals with primitive keys + pure values. Map's
    // construct path Get's [0]/[1] of each entry (own data
    // properties on a fresh Array, no getter) and Map.set's
    // them (primitive key SameValueZero, no user code).
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = (() => 1)();
const b = new Map([["x", 1], ["y", 2]]);
const c = b.get("x") + a;
console.log(c);
export { a, b, c };
"#,
        vec![logical_module("b_module", &[Member::new("b")])],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/b_module.js",
        &["const b = new Map(", r#""x""#],
        &["const a"],
    );
    assert_entry_output(&fixture, "2\n");
}

#[test]
fn class_static_new_map_field() {
    // Class-declaration shape: `static x = new Map()` is the
    // only static-side-effect candidate. Without the rule the
    // class is flagged side-effecting and pulled into the
    // S-chain.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = (() => 1)();
class C {
  static x = new Map();
}
const c = C.x.size + a;
console.log(c);
export { a, C, c };
"#,
        vec![logical_module("c_module", &[Member::new("C")])],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/c_module.js",
        &["class C", "static x = new Map()"],
        &["const a"],
    );
    assert_entry_output(&fixture, "1\n");
}
