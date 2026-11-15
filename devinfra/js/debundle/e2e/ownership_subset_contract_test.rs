//! Ownership subsets keep semantic contracts from the full authored spec.

use debundle_e2e_support::*;

const PURITY_RUNTIME: &str = "globalThis.opaque = value => value;\nexport {};\n";
const PURITY_RUNTIME_CHUNKS: &[(&str, &str)] = &[("static/runtime", PURITY_RUNTIME)];
const PURITY_RUNTIME_FILES: &[(&str, &str)] = &[("static/app/runtime.js", PURITY_RUNTIME)];
const RETAINED_RUNTIME: &str = "globalThis.retained = callback => ({ invoke: callback });\n\
     globalThis.value = () => 42;\n\
     export {};\n";
const RETAINED_FACTORY: &str =
    "export function factory(callback) { return globalThis.retained(callback); }\n";
const IMPORT_OWNER_CHUNKS: &[(&str, &str)] = &[
    ("static/peer", RETAINED_FACTORY),
    ("static/runtime", RETAINED_RUNTIME),
];
const RETAINED_RUNTIME_FILES: &[(&str, &str)] = &[("static/app/runtime.js", RETAINED_RUNTIME)];

fn subset(modules: &str) -> tempfile::NamedTempFile {
    let file = tempfile::NamedTempFile::new().unwrap();
    std::fs::write(file.path(), format!(r#"{{"modules":{modules}}}"#)).unwrap();
    file
}

#[test]
fn omitted_owner_keeps_its_callable_purity_contract() {
    let modules = |purity, colocated| {
        let wrapper = Member::renamed("readableWrapper", "wrapper").with_purity(purity);
        if colocated {
            vec![
                logical_module("functions/omitted", &[wrapper]),
                logical_module(
                    "values/all",
                    &[
                        Member::renamed("readableA", "A"),
                        Member::renamed("readableB", "B"),
                        Member::renamed("readableC", "C"),
                    ],
                ),
            ]
        } else {
            vec![
                logical_module("functions/omitted", &[wrapper]),
                logical_module(
                    "values/outer",
                    &[
                        Member::renamed("readableA", "A"),
                        Member::renamed("readableC", "C"),
                    ],
                ),
                logical_module("values/middle", &[Member::renamed("readableB", "B")]),
            ]
        }
    };
    let options = |purity, colocated| {
        FixtureOpts::new(
            r#"import './runtime.js';
function wrapper(value) { return globalThis.opaque(value); }
const A = wrapper("a");
const B = wrapper("b");
const C = wrapper("c");
console.log(A, B, C);
export { wrapper, A, B, C };
"#,
            modules(purity, colocated),
        )
        .with_extra_chunks(PURITY_RUNTIME_CHUNKS)
        .with_extra_files(PURITY_RUNTIME_FILES)
    };

    let split_selection = subset(
        r#"[{"chunk":"static/app","module":"values/outer"},{"chunk":"static/app","module":"values/middle"}]"#,
    );
    let split_args = [
        "--ownership-subset",
        split_selection.path().to_str().unwrap(),
    ];
    let rejected = run_rejection_fixture_with_args(
        options(MemberPurity::Pure, false),
        &["--ownership-subset", split_args[1], "--emit-on-rejection"],
        3,
    );
    for token in [
        "unrealizable",
        "opaque heap barrier",
        "values/outer",
        "values/middle",
    ] {
        assert!(
            rejected.stderr.contains(token),
            "missing {token:?}: {}",
            rejected.stderr
        );
    }
    assert_node_output(&rejected.source_root.join("static/app.js"), "a b c\n", "");
    assert_node_output(&rejected.entry_path(), "a b c\n", "");
    assert!(rejected.gate_rejected_marker().is_file());

    let colocated_selection = subset(r#"[{"chunk":"static/app","module":"values/all"}]"#);
    let colocated_args = [
        "--ownership-subset",
        colocated_selection.path().to_str().unwrap(),
    ];
    let fixture = run_fixture_with_args(options(MemberPurity::Pure, true), &colocated_args);
    assert_preserved_entry_output(&fixture, "a b c\n");
    assert_generated_module_after_entry_script(
        &fixture,
        "const values = await import('./static/app/modules/values/all.js');\nconsole.log(JSON.stringify(Object.keys(values).sort()));\n",
        "[\"readableA\",\"readableB\",\"readableC\"]\n",
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
        run_rejection_fixture_with_args(options(MemberPurity::Default, false), &split_args, 1);
    assert!(
        rejected.stderr.contains("unrealizable"),
        "{}",
        rejected.stderr
    );
}

#[test]
fn omitted_import_owner_keeps_retained_callback_timing() {
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
        .with_extra_chunks(IMPORT_OWNER_CHUNKS)
        .with_extra_files(RETAINED_RUNTIME_FILES);
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
    let fixture = run_fixture_with_args(options(true), &args);
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
    // The provider body is now a source chunk, so the subset consumer can
    // infer its retained callback behavior even without the explicit export
    // contract. Keep that as a positive control rather than expecting the
    // old opaque-provider refusal.
    let inferred = run_fixture_with_args(options(false), &args);
    assert_preserved_entry_output(&inferred, "42\n");
    assert!(
        !inferred
            .out_root
            .join("static/app/modules/imports/omitted.js")
            .exists()
    );
    assert_module_source(
        &inferred.out_root,
        "static/app/modules/callbacks/wrapped.js",
        &["const readableWrapped", "const callback"],
        &["readableFactory", "const later"],
    );
}
