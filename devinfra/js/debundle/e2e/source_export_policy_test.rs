//! Source-anchored policies do not alias or materialize the provider.
use debundle_e2e_support::*;

const PROVIDER: &str = r#"import { createRequire } from 'node:module';
export const purpose = 'neutral-lazy-registry';
export const version = 'private-callback-storage';
const nativeRequire = import.meta.require ?? createRequire(import.meta.url);
export const resourceUrl = new URL('./resource.cjs', import.meta.url);
export function readResource() { return nativeRequire('./resource.cjs'); }
var factory = (callback) => () => callback();
export { factory };
"#;

#[test]
fn source_policy_projects_without_relocating_context_sensitive_provider() {
    let fixture = run_fixture(FixtureOpts::new(
        "import { factory, readResource, resourceUrl } from './peer.js';\nconst wrapped = factory(() => later);\nconst later = 42;\nconsole.log(wrapped(), readResource(), resourceUrl.pathname.endsWith('/resource.cjs'));\n",
        vec![logical_module("callbacks/wrapped", &[Member::new("wrapped")]), logical_module("values/later", &[Member::new("later")])],
    ).with_extra_chunks(&[("static/peer", PROVIDER)])
        .with_extra_files(&[("static/resource.cjs", "module.exports = 7;\n"), ("static/peer/resource.cjs", "module.exports = 7;\n")])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("lazyRegistry")
            .with_source_markers(&["neutral-lazy-registry", "private-callback-storage"])
            .with_inline_callback_factories(&["factory"])
            .with_pure_exports(&["factory"]).build()]));
    assert_preserved_entry_output(&fixture, "42 7 true\n");
    let control = run_fixture(FixtureOpts::new(
        "import { factory, readResource, resourceUrl } from './peer.js';\nconst wrapped = factory(() => later);\nconst later = 42;\nconsole.log(wrapped(), readResource(), resourceUrl.pathname.endsWith('/resource.cjs'));\n",
        vec![logical_module("together", &[Member::new("wrapped"), Member::new("later")])],
    ).with_extra_chunks(&[("static/peer", PROVIDER)])
        .with_extra_files(&[("static/resource.cjs", "module.exports = 7;\n"), ("static/peer/resource.cjs", "module.exports = 7;\n")]));
    assert_preserved_entry_output(&control, "42 7 true\n");
    assert_same_module_bytes(&control, &fixture, "static/peer/entry.js");
    assert_module_source(
        &fixture.out_root,
        "static/peer/entry.js",
        &["import.meta.require", "import.meta.url"],
        &[],
    );
}

#[test]
fn absent_policy_keeps_opaque_factory_timing() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { factory } from './peer.js';\nconst wrapped = factory(() => later);\nconst later = 42;\nconsole.log(wrapped());\n",
            vec![logical_module("callbacks/wrapped", &[Member::new("wrapped")]), logical_module("values/later", &[Member::new("later")])],
        ).with_extra_chunks(&[("static/peer", PROVIDER)]),
        &["callbacks/wrapped", "values/later"], &[],
    );
}

#[test]
fn source_policy_rejects_no_match_and_ambiguous_loaded_providers() {
    for (chunks, markers, count) in [
        (
            vec![("static/peer", PROVIDER)],
            vec!["missing-purpose"],
            "matched 0",
        ),
        (
            vec![("static/peer", PROVIDER), ("static/unreferenced", PROVIDER)],
            vec!["neutral-lazy-registry", "private-callback-storage"],
            "matched 2",
        ),
    ] {
        expect_rejection_containing_all(
            FixtureOpts::new("console.log(0);\n", vec![])
                .with_extra_chunks(&chunks)
                .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("lazyRegistry")
                    .with_source_markers(&markers)
                    .with_pure_exports(&["factory"])
                    .build()]),
            &[
                "source_markers must match exactly one loaded provider",
                count,
            ],
        );
    }
}

#[test]
fn source_policy_rejects_empty_lists_and_empty_markers() {
    for markers in [
        vec![],
        vec![""],
        vec!["  "],
        vec!["neutral-lazy-registry", ""],
    ] {
        expect_rejection_containing_all(
            FixtureOpts::new("console.log(0);\n", vec![])
                .with_extra_chunks(&[("static/peer", PROVIDER)])
                .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("lazyRegistry")
                    .with_source_markers(&markers)
                    .build()]),
            &["source_markers must contain at least one nonempty marker, and no empty markers"],
        );
    }
}

#[test]
fn source_policy_rejects_existing_id_and_other_source_policy_collisions() {
    for second in [
        ChunkExportPurityBuilder::new("static/peer")
            .with_pure_exports(&["factory"])
            .build(),
        ChunkExportPurityBuilder::new("anotherLabel")
            .with_source_markers(&["private-callback-storage"])
            .build(),
    ] {
        expect_rejection_containing_all(
            FixtureOpts::new("console.log(0);\n", vec![])
                .with_extra_chunks(&[("static/peer", PROVIDER)])
                .with_chunk_export_purity(&[
                    ChunkExportPurityBuilder::new("lazyRegistry")
                        .with_source_markers(&["neutral-lazy-registry"])
                        .build(),
                    second,
                ]),
            &["source_markers collides with another policy for provider static/peer"],
        );
    }
}

#[test]
fn source_markers_are_a_conjunction_and_policy_label_is_not_a_chunk_alias() {
    let fixture = run_fixture(FixtureOpts::new(
        "import { factory } from './peer.js';\nconst wrapped = factory(() => later);\nconst later = 42;\nconsole.log(wrapped());\n",
        vec![logical_module("callbacks/wrapped", &[Member::new("wrapped")]), logical_module("values/later", &[Member::new("later")])],
    ).with_extra_chunks(&[("static/peer", PROVIDER), ("static/other", "export const marker = 'neutral-lazy-registry';\n")])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/other")
            .with_source_markers(&["neutral-lazy-registry", "private-callback-storage"])
            .with_inline_callback_factories(&["factory"]).with_pure_exports(&["factory"]).build()]));
    assert_preserved_entry_output(&fixture, "42\n");
    assert_module_source(
        &fixture.out_root,
        "static/other/entry.js",
        &["neutral-lazy-registry"],
        &["factory"],
    );
}
