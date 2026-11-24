use super::*;
use std::collections::{BTreeMap, BTreeSet};
use std::ffi::OsString;
use std::os::unix::ffi::OsStringExt;
use std::path::PathBuf;

fn projection() -> ApplyProjection {
    let spec = serde_yaml::from_str(r#"
inputs: {input_root: snapshot, js_list_path: js.txt, embedded_loaders_path: ''}
host_behavior:
  synchronous_module_loader: present
  loader_result_for_evaluating_module: returns_partial_namespace
  synchronous_load_runs_app_callbacks: never
  synchronous_load_drains_microtasks: unknown
  reentry_free_builtin_modules: [fs, fs_promises]
  reentry_free_bun_modules: [bun_ffi]
  reentry_free_embedded_loaders: [text]
  reentry_free_operations: [promise_resolve]
  predefined_globals: bun
  note: host audit
program_premises:
  process_prototype_chain_accessors: none
  non_literal_loader_calls: native_addons_only
  intrinsics_never_written: {families: [], note: intrinsic audit}
  note: program audit
vendor:
  swap:
    identity: swap
    level: swap
    package: fake-package
    version: '1'
    subpath: ''
    wrapper_shape: named_from_default
    default_export_aliases: [alias]
  partial:
    identity: partial
    level: partial_swap
    packages: {fake-package: {version: '1', subpath: '', namespace: ''}}
    symbols: {member: {package: fake-package, kind: namespace, upstream_export: '', local: ''}}
  bundled:
    identity: bundled
    level: bundled_partial_swap
    bundle: {path: snapshot/vendor.js}
    packages: {fake-package: {version: '1', subpath: '', bundle_export: api}}
    symbols: {member: {package: fake-package, kind: default}}
logical_modules:
  main:
    values:
      members:
        - {name: '', selector: {binding: {name: value, kind: variable_declarator}}}
        - {selector: {cross_ref: {references: value}}}
        - {selector: {reads_member: {member: field, object: value}}}
        - {selector: {member_of_module: {module: provider, member: value}}}
        - {selector: {passed_to_call: {callee_member: register, arg_index: 0}}}
        - {selector: {makes_decorate_call: {class: Value, member: field}}}
        - {selector: {intrinsic_alias: {property: defineProperty, referenced_by: decorate}}}
      source_matches:
        - match: const value = 1;
          bindings: [value, {local: value, name: ''}]
          note: selector audit
      anonymous_statements:
        - match: register(value);
          no_reentry_call: {callback_arguments: [0, 2]}
          comment: emitted statement
          note: statement audit
      annotations:
        value:
          retained_record_factory: true
          purity: pure_new
          initializer_purity: pure
          initializer_binding_effects: source_only
          initializer_external_effects: private_metadata
          effect: typescript_metadata_decorate_helper
          pure_members: [read]
          no_sync_callback_members: [write]
          comment: emitted binding
          note: annotation audit
      comment: emitted module
      note: module audit
import_order:
  main: [values, helpers/runtime]
  empty: []
chunk_renames:
  main:
    members: [{name: '', selector: {binding: {name: value}}}]
    annotations: {value: {purity: pure}}
unassigned_mode:
  main: {kind: coarse_atomic_groups, complete_partial_atomic_groups_for: [values]}
  inline: {kind: inline_in_entry}
  catchall: {kind: catchall_file, target: ''}
  atomic: {kind: per_atomic_group}
chunk_analysis_options:
  main:
    value_inputs:
      - source_match: {match: 'const value = {};'}
        value: {kind: ordinary_object}
        audited_source_sha256: source
        expected_export: ''
        note: value audit
    native_operation_inputs:
      - resolved_member: {module: values, name: value}
        operation: map_empty
        audited_source_sha256: source
        note: native audit
    commuting_statements:
      - a: {source_match: {match: 'first();'}}
        b: {resolved_member: {module: values, name: value}}
        note: commute audit
    native_call_identity_inputs:
      - source_match: {match: 'const value = Object.freeze;'}
        callee: object_freeze
        audited_source_sha256: source
    argument_inputs:
      - source_match: {match: 'function value(arg) {}'}
        parameter_index: 0
        exact_arity: 1
        audited_source_sha256: source
    no_accessor_install_callables: [{source_match: {match: 'function value() {}'}}]
    deferred_global_publications: {global_this: [value]}
    non_reentrant_global_membership: {window: [value]}
    dataflow_aware_s_chain: true
    trusted_dataflow_summaries: true
    local_property_effects: true
    admission_overrides: [a1_eval, a3_dynamic_import, a5_import_meta]
chunk_export_purity:
  main:
    source_markers: []
    pure_exports: [read]
    retained_callback_factory_members:
      api: {register: [{argument_count: 3, callback_arguments: [0, 2]}]}
    descriptor_copy_factory_exports:
      copy: [{argument_count: 2, object_argument: 1, eager_own_data_keys: [get], assigned_data_keys: [set]}]
swap_vendor_chunks: {output_manifest_path: '', output_wrapper_dir: wrappers, write: false}
materialize_logical_modules: {file: '', prune_other_chunks: true, report_out_dir: reports, target_dir: modules}
write_js_tree: {out_dir: out}
emit_browser_harness: {asset_summary_path: assets.json, out_dir: harness, snapshot_root: snapshot}
"#).unwrap();
    ApplyProjection {
        source: TransformSpecSource::Tree(spec_tree::CompileSpecTreeOptions {
            config_path: "config.yaml".into(),
            modules_root: "modules".into(),
            vendor_marks_path: "vendors.yaml".into(),
            source_root: Some(PathBuf::new()),
            out_root: "out".into(),
        }),
        spec,
        module_roots_by_chunk: BTreeMap::from([(
            "main".into(),
            vec!["modules".into(), PathBuf::new()],
        )]),
        rooted_paths: BTreeSet::from([
            ApplyPath::InputRoot,
            ApplyPath::VendorBundle("bundled".into()),
            ApplyPath::BrowserSnapshot,
        ]),
    }
}

#[test]
fn typed_projection_preserves_spec_content_and_optional_empty_values() {
    let mut original = projection();
    original
        .module_roots_by_chunk
        .insert("empty".into(), Vec::new());
    original.rooted_paths = BTreeSet::from([
        ApplyPath::InputRoot,
        ApplyPath::JsList,
        ApplyPath::EmbeddedLoaders,
        ApplyPath::VendorBundle("bundled".into()),
        ApplyPath::VendorManifest,
        ApplyPath::VendorWrappers,
        ApplyPath::Reports,
        ApplyPath::JsOutput,
        ApplyPath::BrowserAssets,
        ApplyPath::BrowserOutput,
        ApplyPath::BrowserSnapshot,
    ]);
    let decoded = ApplyProjection::decode(&original.encode()).unwrap();
    assert_eq!(
        serde_json::to_value(&original.spec).unwrap(),
        serde_json::to_value(&decoded.spec).unwrap()
    );
    assert_eq!(original.source, decoded.source);
    assert_eq!(
        original.module_roots_by_chunk,
        decoded.module_roots_by_chunk
    );
    assert_eq!(original.rooted_paths, decoded.rooted_paths);
    assert_eq!(original.encode(), decoded.encode());
    assert!(decoded.module_roots_by_chunk.contains_key("empty"));
    assert!(decoded.module_roots_by_chunk["empty"].is_empty());
    assert_eq!(
        decoded.spec.import_order["main"]
            .iter()
            .map(ToString::to_string)
            .collect::<Vec<_>>(),
        ["values", "helpers/runtime"]
    );
    assert!(decoded.spec.import_order["empty"].is_empty());
    assert!(
        decoded.spec.chunk_export_purity["main"]
            .source_markers
            .as_ref()
            .unwrap()
            .is_empty()
    );
    let mut absent = projection();
    absent.module_roots_by_chunk = original.module_roots_by_chunk.clone();
    absent.rooted_paths = original.rooted_paths.clone();
    absent.spec.inputs.embedded_loaders_path = None;
    absent
        .spec
        .chunk_export_purity
        .get_mut("main")
        .unwrap()
        .source_markers = None;
    if let TransformSpecSource::Tree(tree) = &mut absent.source {
        tree.source_root = None;
    }
    assert_ne!(original.encode(), absent.encode());
}

#[test]
fn omitted_import_order_defaults_empty_without_relaxing_spec_schema() {
    let mut value = serde_json::to_value(projection().spec).unwrap();
    value.as_object_mut().unwrap().remove("import_order");
    let decoded: spec::TransformSpec = serde_json::from_value(value.clone()).unwrap();
    assert!(decoded.import_order.is_empty());

    value["unexpected"] = serde_json::Value::Null;
    assert!(serde_json::from_value::<spec::TransformSpec>(value).is_err());
}

#[test]
fn internal_selector_fields_survive_projection_without_serde() {
    let mut original = projection();
    let source_match = spec::SourceMatch {
        identifiers: spec::SourceMatchIdentifierMode::Exact,
        target_binding: Some(String::new()),
        match_source: "const value = 1;".into(),
    };
    let anonymous_statement = &mut original
        .spec
        .logical_modules
        .get_mut("main")
        .unwrap()
        .get_mut("values")
        .unwrap()
        .anonymous_statements[0];
    anonymous_statement.match_source = None;
    anonymous_statement.source_match = Some(source_match.clone());
    let decoded = ApplyProjection::decode(&original.encode()).unwrap();
    assert_eq!(
        decoded.spec.logical_modules["main"]["values"].anonymous_statements[0]
            .source_match
            .as_ref(),
        Some(&source_match)
    );
}

#[test]
fn linux_path_bytes_survive_every_apply_path_field() {
    let path = |suffix| PathBuf::from(OsString::from_vec(vec![b'p', suffix, 0xff]));
    let mut original = projection();
    original.source = TransformSpecSource::Tree(spec_tree::CompileSpecTreeOptions {
        config_path: path(1),
        modules_root: path(2),
        vendor_marks_path: path(3),
        source_root: Some(path(4)),
        out_root: path(5),
    });
    original.spec.inputs.input_root = path(6);
    original.spec.inputs.js_list_path = path(7);
    original.spec.inputs.embedded_loaders_path = Some(path(8));
    if let spec::VendorLevel::BundledPartialSwap(mark) =
        &mut original.spec.vendor.get_mut("bundled").unwrap().level
    {
        mark.bundle.path = path(9);
    } else {
        unreachable!("fixture contains a bundled partial swap");
    }
    original.spec.swap_vendor_chunks.output_manifest_path = Some(path(10));
    original.spec.swap_vendor_chunks.output_wrapper_dir = Some(path(11));
    original.spec.materialize_logical_modules.report_out_dir = Some(path(12));
    original.spec.write_js_tree.as_mut().unwrap().out_dir = path(13);
    let browser = original.spec.emit_browser_harness.as_mut().unwrap();
    browser.asset_summary_path = path(14);
    browser.out_dir = path(15);
    browser.snapshot_root = path(16);
    original
        .module_roots_by_chunk
        .insert("bytes".into(), vec![path(17)]);

    let decoded = ApplyProjection::decode(&original.encode()).unwrap();
    assert_eq!(original.source, decoded.source);
    assert_eq!(
        original.spec.inputs.input_root,
        decoded.spec.inputs.input_root
    );
    assert_eq!(
        original.spec.inputs.js_list_path,
        decoded.spec.inputs.js_list_path
    );
    assert_eq!(
        original.spec.inputs.embedded_loaders_path,
        decoded.spec.inputs.embedded_loaders_path
    );
    let spec::VendorLevel::BundledPartialSwap(original_vendor) =
        &original.spec.vendor["bundled"].level
    else {
        unreachable!("fixture contains a bundled partial swap");
    };
    let spec::VendorLevel::BundledPartialSwap(decoded_vendor) =
        &decoded.spec.vendor["bundled"].level
    else {
        unreachable!("decoded fixture contains a bundled partial swap");
    };
    assert_eq!(original_vendor.bundle.path, decoded_vendor.bundle.path);
    assert_eq!(
        original.spec.swap_vendor_chunks.output_manifest_path,
        decoded.spec.swap_vendor_chunks.output_manifest_path
    );
    assert_eq!(
        original.spec.swap_vendor_chunks.output_wrapper_dir,
        decoded.spec.swap_vendor_chunks.output_wrapper_dir
    );
    assert_eq!(
        original.spec.materialize_logical_modules.report_out_dir,
        decoded.spec.materialize_logical_modules.report_out_dir
    );
    assert_eq!(
        original.spec.write_js_tree.as_ref().unwrap().out_dir,
        decoded.spec.write_js_tree.as_ref().unwrap().out_dir
    );
    assert_eq!(
        original
            .spec
            .emit_browser_harness
            .as_ref()
            .unwrap()
            .asset_summary_path,
        decoded
            .spec
            .emit_browser_harness
            .as_ref()
            .unwrap()
            .asset_summary_path
    );
    assert_eq!(
        original.spec.emit_browser_harness.as_ref().unwrap().out_dir,
        decoded.spec.emit_browser_harness.as_ref().unwrap().out_dir
    );
    assert_eq!(
        original
            .spec
            .emit_browser_harness
            .as_ref()
            .unwrap()
            .snapshot_root,
        decoded
            .spec
            .emit_browser_harness
            .as_ref()
            .unwrap()
            .snapshot_root
    );
    assert_eq!(
        original.module_roots_by_chunk,
        decoded.module_roots_by_chunk
    );
    assert_eq!(original.encode(), decoded.encode());

    let mut flat = projection();
    flat.source = TransformSpecSource::Flat { path: path(18) };
    let flat_decoded = ApplyProjection::decode(&flat.encode()).unwrap();
    assert_eq!(flat.source, flat_decoded.source);
}

#[test]
fn malformed_projection_required_fields_and_version_are_rejected() {
    assert!(ApplyProjection::decode(&[]).is_err());
    let valid = projection().to_proto();
    let mut wrong_version = valid.clone();
    wrong_version.format_version += 1;
    assert!(ApplyProjection::decode(&wrong_version.encode_to_vec()).is_err());
    let mut missing = valid.clone();
    missing.spec = None;
    assert!(ApplyProjection::decode(&missing.encode_to_vec()).is_err());
    let mut missing = valid;
    missing.source = None;
    assert!(ApplyProjection::decode(&missing.encode_to_vec()).is_err());
}
