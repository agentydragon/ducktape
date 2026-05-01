//! Naturalization heuristics: lowered modules should rename scrambled
//! destructured/aliased identifiers to the readable property names that
//! surround them. Black-box: runs `debundle_rust` with a JSONC spec and
//! substring-matches the emitted module file.

use debundle_e2e_support::*;

#[test]
fn renames_destructured_object_params_to_readable_shorthand() {
    let fixture = run_logical_modules_e2e_fixture(
        "renames destructured object params to readable shorthand",
        FixtureOpts::new(
            "function a({ value: n }) { return n; }\n\
             console.log(a({ value: 1 }));\n\
             export { a };\n",
            vec![logical_module("x", &[Member::renamed_func("pair", "a")])],
        ),
    );
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
    let fixture = run_logical_modules_e2e_fixture(
        "keeps outer aliases when nested readable candidates reuse the same target",
        FixtureOpts::new(
            "function z() {\n\
               return \"z\";\n\
             }\n\
             var b = ({\n\
                 p: c\n\
               }) => c,\n\
               f = ({\n\
                 x: a = b\n\
               }) => ({\n\
                 y: a,\n\
                 r: () => a({\n\
                   p: \"p\"\n\
                 })\n\
               }),\n\
               g = f({\n\
                 x: b\n\
               }),\n\
               h = f({\n\
                 q: 1\n\
               });\n\
             console.log(g.r() + h.r() + z());\n\
             export { z, b, f };\n",
            vec![logical_module("z", &[Member::func("z")])],
        ),
    );
    assert_entry_output(&fixture, "ppz\n");
}

#[test]
fn renames_constructor_params_from_this_property_assignments() {
    let fixture = run_logical_modules_e2e_fixture(
        "renames constructor params from this-property assignments",
        FixtureOpts::new(
            "class A {\n\
               constructor(n) { this.value = n; }\n\
             }\n\
             console.log(new A(1).value);\n\
             export { A };\n",
            vec![logical_module("x", &[Member::renamed_class("Pair", "A")])],
        ),
    );
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
    let fixture = run_logical_modules_e2e_fixture(
        "renames return-object aliases to readable shorthand locals",
        FixtureOpts::new(
            "function a(o) {\n\
               const n = o.value;\n\
               return { value: n };\n\
             }\n\
             console.log(JSON.stringify(a({ value: 1 })));\n\
             export { a };\n",
            vec![logical_module("x", &[Member::renamed_func("pair", "a")])],
        ),
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/x.js",
        &["const value = o.value;", "return {", "value", "}"],
        &["value: n"],
    );
    assert_entry_output(&fixture, "{\"value\":1}\n");
}
