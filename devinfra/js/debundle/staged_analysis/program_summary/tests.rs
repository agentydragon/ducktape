use super::*;
use crate::test_support::{collect_test_program_summary, test_bundle};
use analysis_model::{PrimKinds, StableExportValue};
use analysis_vocabulary::inputs::PrimitiveExportCell;
use artifact::reports::ChunkFileRecord;
use artifact::{ChunkBundle, FileMetadata, FileRole, JsFile, JsFileBody};
use program_analysis::analyze_program_shallow;
use spec::ChunkExportPurity;

fn add_non_entry_file(artifact: &mut ChunkBundle, chunk_name: &str, path: &str, source: &str) {
    let chunk_id = artifact.chunk_table.get(chunk_name).expect("test chunk");
    let chunk = artifact
        .chunks
        .iter_mut()
        .find(|chunk| chunk.chunk_id == chunk_id)
        .expect("test artifact chunk");
    chunk.js.files.push(JsFile {
        path: path.to_string(),
        body: JsFileBody::Source(source.to_string()),
        header_lines: Vec::new(),
        binding_comments: BTreeMap::new(),
        leading_item_comments: BTreeMap::new(),
        metadata: FileMetadata {
            chunk_id: chunk_name.to_string(),
            chunk_file: path.to_string(),
            role: FileRole::Module,
            source_path: format!("{chunk_name}/{path}"),
        },
    });
    chunk.analysis.files.push(ChunkFileRecord {
        file: path.to_string(),
        role: FileRole::Module,
    });
}

#[test]
fn request_only_host_loading_preserves_direct_esm_primitive_facts() {
    js_ast::with_swc_globals(|| {
        let parsed = js_ast::parse_js_module(
            "request-only.js",
            "if (false) import.meta.require('./deferred.js');\nexport const label = `tool_${2}`;",
        )
        .expect("request-only ESM source parses");
        let facts = analyze_program_shallow(&parsed).runtime_facts;

        assert!(facts.has_cjs_interop, "the full runtime guard remains set");
        assert!(
            !facts.has_own_cjs_export_surface,
            "request-only loading does not create a CommonJS export surface"
        );
        assert!(facts.stable_export_values.contains_key("label"));

        let parsed = js_ast::parse_js_module(
            "cjs-surface.js",
            "if (false) import.meta.require('./deferred.js');\ntypeof module;\nexport const label = 'tool';",
        )
        .expect("hybrid module source parses");
        let facts = analyze_program_shallow(&parsed).runtime_facts;
        assert!(facts.has_cjs_interop);
        assert!(facts.has_own_cjs_export_surface);
        assert!(!facts.stable_export_values.contains_key("label"));
    });
}

#[test]
fn imported_read_capability_requires_a_resolved_provider_closure_before_the_importer() {
    js_ast::with_swc_globals(|| {
        for (provider, expected_read) in [
            ("export const limit = 2;", true),
            ("import '../app/app.js'; export const limit = 2;", false),
            ("import './missing.js'; export const limit = 2;", false),
        ] {
            let artifact = test_bundle(&[
                (
                    "static/app",
                    "import { limit as bytes } from '../peer/peer.js'; export const copied = bytes;",
                ),
                ("static/peer", provider),
            ]);
            let facts = collect_test_program_summary(&artifact, &BTreeMap::new());
            let provider_chunk = artifact
                .chunks
                .iter()
                .find(|chunk| artifact.chunk_table.name(chunk.chunk_id) == "static/peer")
                .expect("provider source facts");
            assert!(matches!(
                provider_chunk.analysis.runtime_facts.stable_export_values.get("limit"),
                Some(StableExportValue::Local { kinds, .. }) if *kinds == PrimKinds::NUMBER
            ));
            let imported_value = facts.values["static/app"].get("bytes");
            assert_eq!(imported_value.is_some(), expected_read, "{provider}");
            if let Some(imported_value) = imported_value {
                assert_eq!(*imported_value, Value::Prim(PrimKinds::NUMBER));
            }
            let read = facts.primitive_reads["static/app"].get("bytes");
            assert_eq!(read.is_some(), expected_read, "{provider}");
            if !expected_read {
                assert!(facts.values["static/app"].is_empty(), "{provider}");
                assert!(facts.primitive_reads["static/app"].is_empty(), "{provider}");
            }
            if let Some(read) = read {
                assert_eq!(
                    read.dependency_closure,
                    BTreeSet::from(["static/peer".to_string()])
                );
                assert_eq!(
                    read.source_cells,
                    BTreeSet::from([PrimitiveExportCell {
                        module: "static/peer".to_string(),
                        export: "limit".to_string(),
                    }])
                );
            }
        }
    });
}

#[test]
fn primitive_import_facts_do_not_project_through_non_entry_files() {
    js_ast::with_swc_globals(|| {
        let mut artifact = test_bundle(&[
            (
                "static/app",
                "import { value as alias } from '../peer/internal.js';\n\
                     export const copied = alias;\n\
                     export { value as forwarded } from '../peer/internal.js';",
            ),
            ("static/peer", "export const value = 4;"),
        ]);
        add_non_entry_file(
            &mut artifact,
            "static/peer",
            "internal.js",
            "export const value = {};",
        );
        let facts = collect_test_program_summary(&artifact, &BTreeMap::new());

        let app_values = facts
            .values
            .get("static/app")
            .expect("app primitive fact set");
        assert!(!app_values.contains_key("copied"));
        assert!(!app_values.contains_key("forwarded"));
    });
}

#[test]
fn non_entry_synchronous_callback_export_does_not_inherit_entry_summaries() {
    js_ast::with_swc_globals(|| {
        let mut artifact = test_bundle(&[
            (
                "static/app",
                "import { factory } from '../peer/unsafe.js';\n\
                     let later = 0;\n\
                     const callback = () => later;\n\
                     const result = factory(callback);\n\
                     later = 42;\n\
                     console.log(result(), later);",
            ),
            (
                "static/peer",
                "export function factory(callback) { return () => callback(); }",
            ),
        ]);
        add_non_entry_file(
            &mut artifact,
            "static/peer",
            "unsafe.js",
            "export function factory(callback) { callback(); return () => undefined; }",
        );
        let mut assertion = ChunkExportPurity::default();
        assertion.pure_exports.insert("factory".to_string());
        assertion
            .callback_factory_exports
            .insert("factory".to_string());
        let assertions = BTreeMap::from([("static/peer".to_string(), assertion)]);
        let facts = collect_test_program_summary(&artifact, &assertions);

        assert!(
            facts
                .bindings
                .get("static/app")
                .is_none_or(|bindings| !bindings.contains_key("factory"))
        );
        assert!(
            facts
                .imported_callables
                .get("static/app")
                .is_none_or(|bindings| !bindings.contains_key("factory"))
        );
    });
}
