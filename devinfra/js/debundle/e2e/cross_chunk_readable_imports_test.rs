//! Readable names on selected chunks' cross-chunk imports are additive to the
//! legacy chunk export surface and conservative around local collisions.

use debundle_e2e_support::*;

fn named_provider_modules() -> Vec<LogicalModuleEntry> {
    vec![logical_module(
        "provider",
        &[Member::renamed("readable", "p")],
    )]
}

fn setup_named_provider(opts: &mut FixtureOpts<'_>) {
    opts.extra_chunks = &[(
        "static/provider",
        "function p() { return 7; }\nexport { p as h };\n",
    )];
}

fn importer_opts<'a>(source: &'a str, logical_modules: Vec<LogicalModuleEntry>) -> FixtureOpts<'a> {
    FixtureOpts::new(source, logical_modules).with_chunk_id("static/importer")
}

#[test]
fn named_cross_chunk_import_is_readable_and_legacy_consumers_still_work() {
    let mut opts = importer_opts(
        r#"import "./outside.js";
import { h as x } from "./provider.js";
console.log(x());
const guard = 0;
export { x, guard };
"#,
        vec![logical_module("guard", &[Member::new("guard")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    setup_named_provider(&mut opts);
    let provider_modules = named_provider_modules();
    let extra_modules = [("static/provider", provider_modules)];
    opts.extra_chunk_logical_modules = &extra_modules;
    opts.extra_files = &[(
        "static/importer/outside.js",
        r#"import { h } from "../provider/entry.js";
import * as ns from "../provider/entry.js";
console.log(h(), ns.h());
import("../provider/entry.js").then(dynamic => console.log(dynamic.h()));
"#,
    )];
    let fixture = run_fixture(opts);

    assert_entry_output(&fixture, "7 7\n7\n7\n");
    assert_module_source(
        &fixture.out_root,
        "static/importer/entry.js",
        &[
            "import { readable } from \"../provider/entry.js\"",
            "readable()",
        ],
        &["h as x", "x()"],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/provider/entry.js",
        &["h", "readable"],
        &[],
    );
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn source_match_binding_name_is_used_for_cross_chunk_import() {
    let mut opts = importer_opts(
        r#"import { h as x } from "./provider.js";
console.log(x());
const guard = 0;
export { x, guard };
"#,
        vec![logical_module("guard", &[Member::new("guard")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    opts.extra_chunks = &[(
        "static/provider",
        "function p() { return 6; }\nexport { p as h };\n",
    )];
    let provider_modules = vec![logical_module_with_binding_groups(
        "provider",
        &[],
        &[BindingGroup::source_alpha(
            "function local() { return 6; }",
            &[("local", "readable")],
        )],
    )];
    let extra_modules = [("static/provider", provider_modules)];
    opts.extra_chunk_logical_modules = &extra_modules;
    let fixture = run_fixture(opts);

    assert_entry_output(&fixture, "6\n");
    assert_module_source(
        &fixture.out_root,
        "static/importer/entry.js",
        &["import { readable }", "readable()"],
        &["h as x", "x()"],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/provider/entry.js",
        &["h", "readable"],
        &[],
    );
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn conflicting_existing_public_name_does_not_impersonate_a_readable_alias() {
    let mut opts = importer_opts(
        r#"import { h as x } from "./provider.js";
console.log(x());
const guard = 0;
export { x, guard };
"#,
        vec![logical_module("guard", &[Member::new("guard")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    opts.extra_chunks = &[(
        "static/provider",
        "function p() { return 4; }
function q() { return 8; }
export { p as h, q as readable };
",
    )];
    let provider_modules = vec![logical_module(
        "provider",
        &[Member::renamed("readable", "p")],
    )];
    let extra_modules = [("static/provider", provider_modules)];
    opts.extra_chunk_logical_modules = &extra_modules;
    let fixture = run_fixture(opts);

    assert_entry_output(&fixture, "4\n");
    assert_module_source(
        &fixture.out_root,
        "static/importer/entry.js",
        &["import { h as x }", "x()"],
        &["import { readable }"],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/provider/entry.js",
        &["h", "readable"],
        &[],
    );
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn unnamed_target_keeps_its_original_cross_chunk_import_shape() {
    let mut opts = importer_opts(
        r#"import { h as x } from "./provider.js";
console.log(x());
const guard = 0;
export { x, guard };
"#,
        vec![logical_module("guard", &[Member::new("guard")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    opts.extra_chunks = &[(
        "static/provider",
        "function p() { return 5; }\nfunction q() { return 9; }\nexport { p as h, q };\n",
    )];
    let provider_modules = vec![logical_module("provider", &[Member::new("q")])];
    let extra_modules = [("static/provider", provider_modules)];
    opts.extra_chunk_logical_modules = &extra_modules;
    let fixture = run_fixture(opts);

    assert_entry_output(&fixture, "5\n");
    assert_module_source(
        &fixture.out_root,
        "static/importer/entry.js",
        &["import { h as x }", "x()"],
        &["import { p }", "p()"],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/provider/entry.js",
        &["h", "q"],
        &["p"],
    );
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn top_level_name_collision_keeps_import_unchanged() {
    let mut opts = importer_opts(
        r#"import { h as x } from "./provider.js";
const readable = 100;
console.log(x(), readable);
const guard = 0;
export { x, readable, guard };
"#,
        vec![logical_module("guard", &[Member::new("guard")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    setup_named_provider(&mut opts);
    let provider_modules = named_provider_modules();
    let extra_modules = [("static/provider", provider_modules)];
    opts.extra_chunk_logical_modules = &extra_modules;
    let fixture = run_fixture(opts);

    assert_entry_output(&fixture, "7 100\n");
    assert_module_source(
        &fixture.out_root,
        "static/importer/entry.js",
        &["import { h as x }", "x(), readable"],
        &["import { readable }", "readable(), readable"],
    );
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn nested_capture_keeps_import_unchanged() {
    let mut opts = importer_opts(
        r#"import { h as x } from "./provider.js";
function call(readable) { return x() + readable; }
console.log(call(3));
const guard = 0;
export { call, guard };
"#,
        vec![logical_module("guard", &[Member::new("guard")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    setup_named_provider(&mut opts);
    let provider_modules = named_provider_modules();
    let extra_modules = [("static/provider", provider_modules)];
    opts.extra_chunk_logical_modules = &extra_modules;
    let fixture = run_fixture(opts);

    assert_entry_output(&fixture, "10\n");
    assert_module_source(
        &fixture.out_root,
        "static/importer/entry.js",
        &["import { h as x }", "x() + readable"],
        &["import { readable }", "readable() + readable"],
    );
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn two_targets_competing_for_one_readable_local_both_fall_back() {
    let mut opts = importer_opts(
        r#"import { h as x } from "./provider-a.js";
import { q as y } from "./provider-b.js";
console.log(x() + y());
const guard = 0;
export { x, y, guard };
"#,
        vec![logical_module("guard", &[Member::new("guard")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    opts.extra_chunks = &[
        (
            "static/provider-a",
            "function p() { return 2; }\nexport { p as h };\n",
        ),
        (
            "static/provider-b",
            "function q() { return 3; }\nexport { q };\n",
        ),
    ];
    let provider_a = vec![logical_module(
        "provider_a",
        &[Member::renamed("readable", "p")],
    )];
    let provider_b = vec![logical_module(
        "provider_b",
        &[Member::renamed("readable", "q")],
    )];
    let extra_modules = [
        ("static/provider-a", provider_a),
        ("static/provider-b", provider_b),
    ];
    opts.extra_chunk_logical_modules = &extra_modules;
    let fixture = run_fixture(opts);

    assert_entry_output(&fixture, "5\n");
    assert_module_source(
        &fixture.out_root,
        "static/importer/entry.js",
        &["import { h as x }", "import { q as y }", "x() + y()"],
        &["import { readable"],
    );
    assert_all_emitted_js_checks(&fixture);
}

#[test]
fn imports_through_unprocessed_reexport_chunk_keep_legacy_name() {
    let mut opts = importer_opts(
        r#"import { h as x } from "./relay.js";
console.log(x());
const guard = 0;
export { x, guard };
"#,
        vec![logical_module("guard", &[Member::new("guard")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    opts.extra_chunks = &[
        (
            "static/provider",
            "function p() { return 9; }\nexport { p as h };\n",
        ),
        ("static/relay", "export { h } from \"./provider.js\";\n"),
    ];
    let provider_modules = named_provider_modules();
    let extra_modules = [("static/provider", provider_modules)];
    opts.extra_chunk_logical_modules = &extra_modules;
    let fixture = run_fixture(opts);

    assert_entry_output(&fixture, "9\n");
    assert_module_source(
        &fixture.out_root,
        "static/importer/entry.js",
        &["import { h as x }", "x()"],
        &["import { readable }"],
    );
    assert_all_emitted_js_checks(&fixture);
}
