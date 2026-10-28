//! Stable imported primitives cannot execute ToPrimitive callbacks.
use analysis::DepKind;
use debundle_e2e_support::*;

fn source(import: &str, expression: &str) -> String {
    format!(
        "{import}\nlet later = 0;\nconst callback = () => later;\n\
         globalThis.saved = callback;\nconst computed = {expression};\n\
         later = 42;\nconsole.log(computed, callback());\n"
    )
}

fn split_modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module_with_anon(
            "compute",
            &[Member::new("callback"), Member::new("computed")],
            &["globalThis.saved = callback;"],
        ),
        logical_module_with_anon("later", &[Member::new("later")], &["later = 42;"]),
    ]
}

fn assert_primitive(peer: &str, import: &str, expression: &str, expected: &str) {
    let source = source(import, expression);
    let peers = [("static/peer", peer)];
    let fixture = run_fixture(FixtureOpts::new(&source, split_modules()).with_extra_chunks(&peers));
    assert_preserved_entry_output(&fixture, expected);
    let graph = fixture.dependency_graph();
    let computed = source_unit_for_binding(&graph, "computed");
    let later = source_unit_for_binding(&graph, "later");
    // External import targets have no node in the single-chunk graph. The emit
    // must retain the import and binary initializer rather than fold its value.
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/compute.js",
        &["import", "bytes"],
        &[],
    );
    assert_module_variable_declarators(
        &fixture.out_root,
        "static/app/modules/compute.js",
        &[
            (
                VariableDeclarationKind::Const,
                &["callback"],
                VariableInitializerKind::Arrow,
            ),
            (
                VariableDeclarationKind::Const,
                &["computed"],
                VariableInitializerKind::Binary,
            ),
        ],
    );
    assert!(!graph.edges.iter().any(|edge| {
        edge.source == computed
            && edge.target == later
            && edge.edge_kind == DepKind::EagerUse
            && edge.constrains_init_order
    }));
    let node = graph.nodes.iter().find(|node| node.id == computed).unwrap();
    assert!(
        !node.purity.is_pure(),
        "primitive inference is not a purity assertion"
    );
}

#[test]
fn direct_literal_const_import_does_not_promote_unrelated_callbacks() {
    assert_primitive(
        "export const limit = 2;\n",
        "import { limit as bytes } from './peer.js';",
        "4 * bytes + 256",
        "264 42\n",
    );
}

#[test]
fn imported_template_and_numeric_values_survive_copy_and_reexport_chains() {
    let source = "import { prefix, forwardedPrefix, limit } from './bridge.js';\n\
                  let later = 0;\nconst callback = () => later;\n\
                  globalThis.saved = callback;\n\
                  const computed = `${prefix}${forwardedPrefix}${4 * limit + 256}`;\n\
                  later = 42;\nconsole.log(computed, callback());\n";
    let peers = [
        (
            "static/bridge",
            "import { prefix as providerPrefix, limit } from './provider.js';\n\
             const copiedPrefix = providerPrefix;\n\
             const copiedLimit = limit;\n\
             export { copiedPrefix as prefix, copiedLimit as limit };\n\
             export { prefix as forwardedPrefix } from './provider.js';\n",
        ),
        (
            "static/provider",
            "if (false) import.meta.require('./deferred-host-module.js');\n\
             const family = { [Symbol.toPrimitive]() { console.log('coerce'); return 'notes'; } };\n\
             export const prefix = `tool_${family}_`;\n\
             const amount = 2 * 3;\n\
             export const limit = amount;\n",
        ),
    ];
    let fixture = run_fixture(FixtureOpts::new(source, split_modules()).with_extra_chunks(&peers));
    assert_preserved_entry_output(&fixture, "coerce\ntool_notes_tool_notes_280 42\n");

    let graph = fixture.dependency_graph();
    let computed = source_unit_for_binding(&graph, "computed");
    let later = source_unit_for_binding(&graph, "later");
    assert!(!graph.edges.iter().any(|edge| {
        edge.source == computed
            && edge.target == later
            && edge.edge_kind == DepKind::EagerUse
            && edge.constrains_init_order
    }));
    assert!(
        !graph
            .nodes
            .iter()
            .find(|node| node.id == computed)
            .unwrap()
            .purity
            .is_pure(),
        "primitive result inference must not erase coercion effects"
    );
}

#[test]
fn immutable_var_and_direct_export_aliases_are_primitive() {
    for peer in [
        "export var limit = 2;\n",
        "var value = 2; export { value as limit };\n",
        "const value = 2; export { value as limit };\n",
    ] {
        assert_primitive(
            peer,
            "import { limit as bytes } from './peer.js';",
            "4 * bytes + 256",
            "264 42\n",
        );
    }
}

#[test]
fn direct_default_literals_and_bindings_are_primitive() {
    for peer in [
        "export default 2;\n",
        "const value = 2; export default value;\n",
        "var value = 2; export { value as default };\n",
    ] {
        assert_primitive(
            peer,
            "import bytes from './peer.js';",
            "4 * bytes + 256",
            "264 42\n",
        );
    }
}

#[test]
fn other_literal_primitives_skip_callbacks_without_folding_operations() {
    for (literal, expression, expected) in [
        ("'two'", "'prefix:' + bytes", "prefix:two 42\n"),
        ("true", "4 * bytes + 256", "260 42\n"),
        ("null", "4 * bytes + 256", "256 42\n"),
        ("2n", "4n * bytes + 256n", "264n 42\n"),
    ] {
        let peer = format!("export const limit = {literal};\n");
        assert_primitive(
            &peer,
            "import { limit as bytes } from './peer.js';",
            expression,
            expected,
        );
    }
}

fn assert_conservative(peer: &str) {
    let source = source(
        "import { limit as bytes } from './peer.js';",
        "4 * bytes + 256",
    );
    let peers = [
        ("static/peer", peer),
        ("static/base", "export const value = 2;\n"),
    ];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(&source, split_modules()).with_extra_chunks(&peers),
        &["compute", "later"],
        &["eager_use"],
    );
    let fixture = run_fixture(
        FixtureOpts::new(
            &source,
            vec![logical_module_with_anon(
                "together",
                &[
                    Member::new("callback"),
                    Member::new("computed"),
                    Member::new("later"),
                ],
                &["globalThis.saved = callback;", "later = 42;"],
            )],
        )
        .with_extra_chunks(&peers),
    );
    assert_preserved_entry_output(&fixture, "264 42\n");
}

#[test]
fn mutable_opaque_and_indirect_exports_stay_conservative() {
    for peer in [
        "export let limit = 2;\n",
        "export var limit = 2; limit = { valueOf() { return 2; } };\n",
        "export var limit = 2; export function change() { limit = { valueOf() { return 2; } }; }\n",
        "export var limit = 2; [limit] = [2];\n",
        "export var limit = 2; for (limit of [2]) {}\n",
        "export var limit = 2; var limit;\n",
        "export const limit = { valueOf() { return 2; } };\n",
        "export const { limit } = { limit: 2 };\n",
        "export const limit = 2; void typeof module;\n",
    ] {
        assert_conservative(peer);
    }
}

fn assert_imported_chain_conservative(bridge: &str, provider: &str) {
    let app_source = source(
        "import { limit as bytes } from './bridge.js';",
        "4 * bytes + 256",
    );
    let peers = [("static/bridge", bridge), ("static/provider", provider)];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(&app_source, split_modules()).with_extra_chunks(&peers),
        &["compute", "later"],
        &["eager_use"],
    );
}

#[test]
fn conditional_and_logical_results_require_primitive_value_alternatives() {
    let app_source = source(
        "import { conditional, logical } from './bridge.js';",
        "4 * conditional + logical",
    );
    let peers = [
        (
            "static/bridge",
            "import { limit } from './provider.js';\n\
             const conditional = globalThis.flag ? limit : 3;\n\
             const logical = limit && 2;\n\
             export { conditional, logical };\n",
        ),
        ("static/provider", "export const limit = 6;\n"),
    ];
    let fixture =
        run_fixture(FixtureOpts::new(&app_source, split_modules()).with_extra_chunks(&peers));
    assert_preserved_entry_output(&fixture, "14 42\n");
}

#[test]
fn object_branches_rebinding_accessors_and_unseeded_import_cycles_stay_unknown() {
    let provider = "export const limit = 2;\n";
    assert_imported_chain_conservative(
        "import { limit } from './provider.js';\n\
         export const copied = globalThis.flag ? limit : { valueOf() { return 2; } };\n\
         export { copied as limit };\n",
        provider,
    );
    assert_imported_chain_conservative(
        "import { limit } from './provider.js';\n\
         export const copied = globalThis.flag && { valueOf() { return 2; } };\n\
         export { copied as limit };\n",
        provider,
    );
    assert_imported_chain_conservative(
        "import { limit } from './provider.js';\n\
         var copied = limit;\n\
         copied = { valueOf() { return 2; } };\n\
         export { copied as limit };\n",
        provider,
    );
    assert_imported_chain_conservative(
        "import { limit as sourceLimit } from './provider.js';\n\
         var limit = sourceLimit;\n\
         void typeof module;\n\
         Object.defineProperty(exports, 'limit', { get() { return { valueOf() { return 2; } }; } });\n\
         export { limit };\n",
        provider,
    );
    assert_imported_chain_conservative(
        "import { value as back } from './provider.js';\n\
         const result = back;\n\
         export { result as limit };\n",
        "import { limit } from './bridge.js';\n\
         export const value = limit;\n",
    );
}

#[test]
fn direct_eval_in_lazy_provider_function_disables_inference() {
    assert_conservative("export var limit = 2; export function change() { eval('limit = {}'); }\n");
}

#[test]
fn namespace_property_is_not_a_direct_imported_binding() {
    let source = source(
        "import * as namespace from './peer.js';",
        "4 * namespace.limit + 256",
    );
    expect_atomic_conflict_rejection(
        FixtureOpts::new(&source, split_modules())
            .with_extra_chunks(&[("static/peer", "export const limit = 2;\n")]),
        &["compute", "later"],
        &["eager_use"],
    );
}

#[test]
fn shadowed_import_name_does_not_certify_an_object_parameter() {
    let source = "import { limit as bytes } from './peer.js';\n\
                  let later = 0;\nconst callback = () => later;\n\
                  globalThis.saved = callback;\n\
                  function coerce(bytes) { return 4 * bytes + 256; }\n\
                  const computed = coerce({ valueOf() { return 2; } });\n\
                  later = 42;\nconsole.log(computed, callback());\n";
    let mut modules = split_modules();
    modules[0] = logical_module_with_anon(
        "compute",
        &[
            Member::new("callback"),
            Member::new("coerce"),
            Member::new("computed"),
        ],
        &["globalThis.saved = callback;"],
    );
    expect_atomic_conflict_rejection(
        FixtureOpts::new(source, modules)
            .with_extra_chunks(&[("static/peer", "export const limit = 2;\n")]),
        &["compute", "later"],
        &["eager_use"],
    );
}

#[test]
fn mixed_bigint_arithmetic_keeps_the_runtime_throw() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { limit as bytes } from './peer.js';\n\
         let result;\ntry { result = 4 * bytes; } catch (error) { result = error.name; }\n\
         console.log(result);\n",
            vec![logical_module_with_anon(
                "compute",
                &[Member::new("result")],
                &["try { result = 4 * bytes; } catch (error) { result = error.name; }"],
            )],
        )
        .with_extra_chunks(&[("static/peer", "export const limit = 2n;\n")]),
    );
    assert_preserved_entry_output(&fixture, "TypeError\n");
}

#[test]
fn selected_provider_uses_its_canonical_primitive_facts() {
    let source = source(
        "import { limit as bytes } from './peer.js';",
        "4 * bytes + 256",
    );
    let peers = [("static/peer", "export const limit = 2;\n")];
    let peer_modules = [(
        "static/peer",
        vec![logical_module("limits", &[Member::new("limit")])],
    )];
    let fixture = run_fixture(
        FixtureOpts::new(&source, split_modules())
            .with_extra_chunks(&peers)
            .with_extra_chunk_logical_modules(&peer_modules),
    );
    assert_preserved_entry_output(&fixture, "264 42\n");
}

#[test]
fn vendor_affected_provider_does_not_reuse_original_primitive_facts() {
    let source = source(
        "import { limit as bytes } from './peer.js';",
        "4 * bytes + 256",
    );
    let peers = [("static/peer", "export const limit = 2;\n")];
    let vendor_marks = [(
        "static/peer.js",
        VendorMark {
            identity: "neutral-provider".into(),
            role: Default::default(),
            level: VendorLevel::Suppress,
        },
    )];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(&source, split_modules())
            .with_extra_chunks(&peers)
            .with_vendor_marks(&vendor_marks),
        &["compute", "later"],
        &["eager_use"],
    );
}
