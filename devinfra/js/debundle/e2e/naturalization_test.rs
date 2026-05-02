//! Naturalization heuristics: lowered modules should rename scrambled
//! destructured/aliased identifiers to the readable property names that
//! surround them. Black-box: runs `debundle` with a JSONC spec and
//! substring-matches the emitted module file.

use debundle_e2e_support::*;

#[test]
fn renames_destructured_object_params_to_readable_shorthand() {
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"function a({ value: n }) { return n; }
console.log(a({ value: 1 }));
export { a };
"#,
        vec![logical_module("x", &[Member::renamed("pair", "a")])],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/x.js",
        &["function pair({", "value", "return value;"],
        &["value: n"],
    );
    assert_entry_output(&fixture, "1\n");
}

#[test]
fn keeps_outer_aliases_when_nested_readable_candidates_reuse_the_same_target() {
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"function z() {
  return "z";
}
var b = ({
    p: c
  }) => c,
  f = ({
    x: a = b
  }) => ({
    y: a,
    r: () => a({
      p: "p"
    })
  }),
  g = f({
    x: b
  }),
  h = f({
    q: 1
  });
console.log(g.r() + h.r() + z());
export { z, b, f };
"#,
        vec![logical_module("z", &[Member::new("z")])],
    ));
    assert_entry_output(&fixture, "ppz\n");
}

#[test]
fn renames_constructor_params_from_this_property_assignments() {
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"class A {
  constructor(n) { this.value = n; }
}
console.log(new A(1).value);
export { A };
"#,
        vec![logical_module("x", &[Member::renamed("Pair", "A")])],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/x.js",
        &["constructor(value)", "this.value = value;"],
        &["constructor(n)"],
    );
    assert_entry_output(&fixture, "1\n");
}

#[test]
fn renames_return_object_aliases_to_readable_shorthand_locals() {
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"function a(o) {
  const n = o.value;
  return { value: n };
}
console.log(JSON.stringify(a({ value: 1 })));
export { a };
"#,
        vec![logical_module("x", &[Member::renamed("pair", "a")])],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/x.js",
        &["const value = o.value;", "return {", "value", "}"],
        &["value: n"],
    );
    assert_entry_output(&fixture, "{\"value\":1}\n");
}
