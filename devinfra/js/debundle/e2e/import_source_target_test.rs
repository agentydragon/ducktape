//! Named import claims must retain whether their source is external or an
//! indexed output path when moved into a logical module.

use debundle_e2e_support::*;

fn import_claim(binding: &'static str) -> Member {
    Member::renamed_with_kind(binding, binding, Some("import_specifier"))
}

#[test]
fn node_builtin_import_keeps_its_specifier_when_moved_with_a_local_binding() {
    let fixture = run_fixture(FixtureOpts::new(
        "const live = 1;\nimport { basename as dep } from 'node:path';\n\
         console.log(live, dep('/tmp/example'));\n",
        vec![logical_module(
            "alpha",
            &[Member::new("live"), import_claim("dep")],
        )],
    ));

    assert_preserved_entry_output(&fixture, "1 example\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/alpha.js",
        &["node:path"],
        &["../node:path"],
    );
}
