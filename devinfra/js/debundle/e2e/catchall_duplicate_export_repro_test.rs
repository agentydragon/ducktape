//! A catchall module must preserve a chunk's public exports exactly once.

use debundle_e2e_support::*;

#[test]
fn cross_chunk_read_keeps_catchall_export_unique() {
    let extra_chunks = [("static/consumer", "import { a } from './app.js';\na;\n")];
    let fixture = run_fixture(
        FixtureOpts::new("const a = 1;\nexport { a };\n", vec![]).with_extra_chunks(&extra_chunks),
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &["const a"],
        &[],
    );
    assert_module_exports(&fixture.out_root, "static/app/entry.js", &["a"], &[]);
}
