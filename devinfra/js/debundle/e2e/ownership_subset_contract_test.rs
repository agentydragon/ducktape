//! Ownership subsets keep semantic contracts from the full authored spec.

use debundle_e2e_support::*;

fn subset(modules: &str) -> tempfile::NamedTempFile {
    let file = tempfile::NamedTempFile::new().unwrap();
    std::fs::write(file.path(), format!(r#"{{"modules":{modules}}}"#)).unwrap();
    file
}

#[test]
fn omitted_owner_keeps_its_callable_purity_contract() {
    const RUNTIME: &str = "globalThis.opaque = value => value;\nexport {};\n";
    let options = |purity| {
        FixtureOpts::new(
            r#"import './runtime.js';
function wrapper(value) { return globalThis.opaque(value); }
const A = wrapper("a");
const B = wrapper("b");
const C = wrapper("c");
console.log(A, B, C);
export { wrapper, A, B, C };
"#,
            vec![
                logical_module(
                    "functions/omitted",
                    &[Member::renamed("readableWrapper", "wrapper").with_purity(purity)],
                ),
                logical_module(
                    "values/outer",
                    &[
                        Member::renamed("readableA", "A"),
                        Member::renamed("readableC", "C"),
                    ],
                ),
                logical_module("values/middle", &[Member::renamed("readableB", "B")]),
            ],
        )
        .with_extra_files(&[
            ("static/runtime.js", RUNTIME),
            ("static/app/runtime.js", RUNTIME),
        ])
    };
    let selection = subset(
        r#"[{"chunk":"static/app","module":"values/outer"},{"chunk":"static/app","module":"values/middle"}]"#,
    );
    let args = ["--ownership-subset", selection.path().to_str().unwrap()];
    let fixture = run_fixture_without_graphs_with_args(options(MemberPurity::Pure), &args);
    assert_preserved_entry_output(&fixture, "a b c\n");
    assert_generated_module_after_entry_script(
        &fixture,
        "const outer = await import('./static/app/modules/values/outer.js');\nconst middle = await import('./static/app/modules/values/middle.js');\nconsole.log(JSON.stringify([Object.keys(outer).sort(), Object.keys(middle).sort()]));\n",
        "[[\"readableA\",\"readableC\"],[\"readableB\"]]\n",
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &["function wrapper"],
        &["readableWrapper", "const A", "const B", "const C"],
    );
    assert!(
        !fixture
            .out_root
            .join("static/app/modules/functions/omitted.js")
            .exists()
    );
    let rejected =
        run_rejection_fixture_without_graphs_with_args(options(MemberPurity::Default), &args, 1);
    assert!(
        rejected.stderr.contains("unrealizable"),
        "{}",
        rejected.stderr
    );
}

#[test]
fn omitted_import_owner_keeps_retained_callback_timing() {
    const RUNTIME: &str = "globalThis.retained = callback => ({ invoke: callback });\nglobalThis.value = () => 42;\nexport {};\n";
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_retained_callback_factory("factory", 1, &[0])
        .build()];
    let options = |assert_retention| {
        let options = FixtureOpts::new(
            r#"import './runtime.js';
import { factory as make } from './peer.js';
const callback = () => later;
const wrapped = make(callback);
const later = globalThis.value();
console.log(wrapped.invoke());
export { wrapped, later };
"#,
            vec![
                logical_module(
                    "imports/omitted",
                    &[Member::renamed_with_kind(
                        "readableFactory",
                        "make",
                        Some("import_specifier"),
                    )],
                ),
                logical_module(
                    "callbacks/wrapped",
                    &[
                        Member::new("callback"),
                        Member::renamed("readableWrapped", "wrapped"),
                    ],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export function factory(callback) { return globalThis.retained(callback); }\n",
        )])
        .with_extra_files(&[
            ("static/runtime.js", RUNTIME),
            ("static/app/runtime.js", RUNTIME),
        ]);
        if assert_retention {
            options.with_chunk_export_purity(&contracts)
        } else {
            options
        }
    };
    let selection = subset(
        r#"[{"chunk":"static/app","module":"callbacks/wrapped"},{"chunk":"static/app","module":"values/later"}]"#,
    );
    let args = ["--ownership-subset", selection.path().to_str().unwrap()];
    let fixture = run_fixture_without_graphs_with_args(options(true), &args);
    assert_preserved_entry_output(&fixture, "42\n");
    assert!(
        !fixture
            .out_root
            .join("static/app/modules/imports/omitted.js")
            .exists()
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/callbacks/wrapped.js",
        &["const readableWrapped", "const callback"],
        &["readableFactory", "const later"],
    );
    let rejected = run_rejection_fixture_without_graphs_with_args(options(false), &args, 1);
    assert!(
        rejected.stderr.contains("unrealizable"),
        "{}",
        rejected.stderr
    );
}
