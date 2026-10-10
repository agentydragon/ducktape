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
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &["a"],
        &[],
    );
    assert_generated_module_after_entry_script(
        &fixture,
        "const { a } = await import('./static/app/entry.js');\nconsole.log(a);\n",
        "1\n",
    );
}

#[test]
fn catchall_keeps_aliased_public_export_without_known_consumer() {
    let fixture = run_fixture(FixtureOpts::new(
        "const a = 1;\nexport { a as foo, a as 'x-y' };\n",
        vec![],
    ));
    assert_module_exports(
        &fixture.out_root,
        "static/app/entry.js",
        &["foo", "x-y"],
        &[],
    );
    assert_generated_module_after_entry_script(
        &fixture,
        "const mod = await import('./static/app/entry.js');\nconsole.log(mod.foo, mod['x-y']);\n",
        "1 1\n",
    );
}

#[test]
fn named_export_claimed_with_renamed_binding_keeps_public_surface() {
    let fixture = run_fixture(FixtureOpts::new(
        "const a = 1;\nexport { a as foo };\n",
        vec![logical_module_with_anon(
            "residual/unhandled",
            &[Member::renamed("foo", "a")],
            &["export { a as foo };"],
        )],
    ));
    assert_module_exports(&fixture.out_root, "static/app/entry.js", &["foo"], &[]);
    assert_generated_module_after_entry_script(
        &fixture,
        "const { foo } = await import('./static/app/entry.js');\nconsole.log(foo);\n",
        "1\n",
    );
}
