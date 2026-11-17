//! Class-provenance preparation runs even without a namespace capability.
//! Keep the shared false paths as source input to the real CLI, and check
//! fresh/saved output behavior rather than treating work counts as that oracle.

use debundle_e2e_support::saved_analysis::assert_staged_entry_output;
use debundle_e2e_support::*;

#[test]
fn repeated_conditional_arms_without_classes_preserve_fresh_and_saved_behavior() {
    let mut source = "const flag = false; const a0 = {};\n".to_owned();
    for index in 1..=14 {
        source.push_str(&format!(
            "const a{index} = flag ? a{} : a{};\n",
            index - 1,
            index - 1
        ));
    }
    source.push_str("console.log(a14 === a0);\n");
    assert_staged_entry_output(
        FixtureOpts::new(&source, vec![]).with_import_order(&["residual"]),
        "true\n",
    );
}

#[test]
fn shared_class_diamond_retains_constructor_and_instance_behavior() {
    let source = "const flag = false;\n\
        class Base { value = 7; }\n\
        const left = flag ? Base : Base;\n\
        const right = Base;\n\
        const joined = flag ? left : right;\n\
        const instance = new joined();\n\
        console.log(joined === Base, instance.value);\n";
    assert_staged_entry_output(
        FixtureOpts::new(
            source,
            vec![logical_module(
                "classes",
                &[
                    Member::new("flag"),
                    Member::new("Base"),
                    Member::new("left"),
                    Member::new("right"),
                    Member::new("joined"),
                    Member::new("instance"),
                ],
            )],
        )
        .with_import_order(&["classes", "residual"]),
        "true 7\n",
    );
}

#[test]
fn class_diamond_publishing_namespace_keeps_early_callback_effects() {
    let source = "import { api } from './peer.js';\n\
        const flag = false;\n\
        let later = 0;\n\
        function eager(callback) { const value = callback(); return { invoke: () => value }; }\n\
        class Holder { static target = api; }\n\
        const left = flag ? Holder : Holder;\n\
        const right = Holder;\n\
        const joined = flag ? left : right;\n\
        joined.target.make = eager;\n\
        const wrapped = api.make(() => later);\n\
        later = 42;\nconsole.log(wrapped.invoke());\n";
    let peers = [(
        "static/peer",
        "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
    )];
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_callback_factory_members("api", &["make"])
        .with_namespace_independent_members("api", &["make"])
        .build()];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "callbacks",
                    &[
                        Member::new("flag"),
                        Member::new("eager"),
                        Member::new("Holder"),
                        Member::new("left"),
                        Member::new("right"),
                        Member::new("joined"),
                        Member::new("wrapped"),
                    ],
                    &["joined.target.make = eager;"],
                ),
                logical_module_with_anon("values", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_import_order(&["callbacks", "values"])
        .with_extra_chunks(&peers)
        .with_chunk_export_purity(&contracts),
        &["callbacks", "values"],
        &[],
    );
    assert_staged_entry_output(
        FixtureOpts::new(source, vec![])
            .with_import_order(&["residual"])
            .with_extra_chunks(&peers)
            .with_chunk_export_purity(&contracts),
        "0\n",
    );
}
