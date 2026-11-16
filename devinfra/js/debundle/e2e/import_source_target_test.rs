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

#[test]
fn relocating_a_bare_package_keeps_the_existing_loader_scope_refusal() {
    let files = [
        (
            "node_modules/toolkit/package.json",
            r#"{"type":"module","exports":"./index.js"}"#,
        ),
        (
            "node_modules/toolkit/index.js",
            "export const identify = () => 'root';",
        ),
        (
            "static/app/modules/node_modules/toolkit/package.json",
            r#"{"type":"module","exports":"./index.js"}"#,
        ),
        (
            "static/app/modules/node_modules/toolkit/index.js",
            "export const identify = () => 'nested';",
        ),
    ];
    let rejected = run_emit_on_rejection_fixture(
        FixtureOpts::new(
            "import { identify as dep } from 'toolkit'; const live = 1; console.log(live, dep());",
            vec![logical_module(
                "alpha",
                &[Member::new("live"), import_claim("dep")],
            )],
        )
        .with_extra_files(&files),
    );

    assert!(
        rejected
            .stderr
            .contains("evaluation_schedule_owner_mismatch"),
        "{}",
        rejected.stderr
    );
    let emitted_module = rejected.app_root().join("static/app/modules/alpha.js");
    assert!(
        emitted_module.exists(),
        "emit-on-rejection must retain the candidate tree"
    );
    assert!(rejected.gate_rejected_marker().exists());
    assert_module_source(
        &rejected.app_root(),
        "static/app/modules/alpha.js",
        &["from \"toolkit\""],
        &["../toolkit"],
    );
}
