use super::*;
use spec_wire_proto::debundle::spec as spec_pb;
use std::collections::{BTreeMap, BTreeSet};
use std::ffi::OsString;
use std::os::unix::ffi::OsStringExt;

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
    pure_members: {api: [read]}
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
        format_version: super::super::APPLY_PROJECTION_FORMAT_VERSION,
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
    let original = projection();
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
    assert!(
        decoded.spec.chunk_export_purity["main"]
            .source_markers
            .as_ref()
            .unwrap()
            .is_empty()
    );
    let mut absent = projection();
    absent
        .spec
        .chunk_export_purity
        .get_mut("main")
        .unwrap()
        .source_markers = None;
    assert_ne!(original.encode(), absent.encode());
}

#[test]
fn internal_selector_fields_and_linux_path_bytes_survive() {
    let original = spec::SourceMatch {
        identifiers: spec::SourceMatchIdentifierMode::Exact,
        target_binding: Some(String::new()),
        match_source: "const value = 1;".into(),
    };
    assert_eq!(
        original,
        spec::SourceMatch::from_proto(original.to_proto()).unwrap()
    );
    let path = PathBuf::from(OsString::from_vec(b"non-utf8-\xff".to_vec()));
    assert_eq!(path, PathBuf::from_proto(path.to_proto()).unwrap());
}

#[test]
fn malformed_projection_fields_and_callbacks_are_rejected() {
    assert!(ApplyProjection::decode(b"{}").is_err());
    assert!(ApplyProjection::decode(&[]).is_err());
    let valid = projection().to_proto();
    let mut wrong_version = valid.clone();
    wrong_version.format_version += 1;
    assert!(ApplyProjection::decode(&wrong_version.encode_to_vec()).is_err());
    let mut missing = valid.clone();
    missing.spec = None;
    assert!(ApplyProjection::decode(&missing.encode_to_vec()).is_err());
    let mut unknown = valid;
    unknown
        .spec
        .as_mut()
        .unwrap()
        .host_behavior
        .as_mut()
        .unwrap()
        .synchronous_module_loader = Some(i32::MAX);
    assert!(ApplyProjection::decode(&unknown.encode_to_vec()).is_err());
    for positions in [vec![], vec![1, 1]] {
        assert!(
            spec::NoReentryCall::from_proto(spec_pb::NoReentryCall {
                callback_arguments: positions,
            })
            .is_err()
        );
    }
    for positions in [vec![], vec![2]] {
        assert!(
            spec::RetainedCallbackSignature::from_proto(spec_pb::RetainedCallbackSignature {
                argument_count: Some(2),
                callback_arguments: positions,
            })
            .is_err()
        );
    }
}

#[test]
fn protobuf_maps_use_last_value_and_reencode_in_canonical_order() {
    let base = projection().to_proto().encode_to_vec();
    let entry = |key: &str, path: &[u8]| {
        wire::ApplyProjection {
            module_roots_by_chunk: BTreeMap::from([(
                key.to_string(),
                wire::ModuleRoots {
                    paths: vec![path.to_vec()],
                },
            )]),
            ..wire::ApplyProjection::default()
        }
        .encode_to_vec()
    };
    let mut first = base.clone();
    first.extend(entry("z", b"obsolete"));
    first.extend(entry("a", b"a-path"));
    first.extend(entry("z", b"z-\xff"));
    let mut second = base;
    second.extend(entry("z", b"z-\xff"));
    second.extend(entry("a", b"a-path"));
    let first = ApplyProjection::decode(&first).unwrap();
    let second = ApplyProjection::decode(&second).unwrap();
    assert_eq!(first.module_roots_by_chunk["z"][0].to_proto(), b"z-\xff");
    assert_eq!(first.encode(), second.encode());
    assert_eq!(
        first.encode(),
        ApplyProjection::decode(&first.encode()).unwrap().encode()
    );
}

#[test]
fn empty_collections_and_linux_paths_preserve_projection_meaning() {
    let mut original = projection();
    original
        .module_roots_by_chunk
        .insert("empty".into(), Vec::new());
    original.spec.inputs.input_root = PathBuf::from(OsString::from_vec(b"root-\xff".to_vec()));
    let decoded = ApplyProjection::decode(&original.encode()).unwrap();
    assert!(decoded.module_roots_by_chunk.contains_key("empty"));
    assert!(decoded.module_roots_by_chunk["empty"].is_empty());
    assert_eq!(
        original.spec.inputs.input_root,
        decoded.spec.inputs.input_root
    );
    assert_eq!(original.encode(), decoded.encode());
}

#[test]
fn empty_flat_path_and_listed_intrinsics_keep_oneof_presence() {
    let mut original = projection();
    original.source = TransformSpecSource::Flat {
        path: PathBuf::new(),
    };
    let decoded = ApplyProjection::decode(&original.encode()).unwrap();
    assert_eq!(original.source, decoded.source);
    assert_eq!(original.encode(), decoded.encode());
    let listed = original.encode();
    original
        .spec
        .program_premises
        .intrinsics_never_written
        .as_mut()
        .unwrap()
        .families = spec::IntrinsicFamilies::All(spec::AllIntrinsicFamilies::All);
    assert_ne!(listed, original.encode());
}
