use super::*;
use crate::AnalysisSpec;

fn projection() -> AnalysisSpec {
    let spec: spec::TransformSpec = serde_yaml::from_str(
            r#"
inputs:
  input_root: source
  js_list_path: chunks.json
host_behavior:
  synchronous_module_loader: present
  synchronous_load_runs_app_callbacks: never
  reentry_free_builtin_modules: [fs, path]
  reentry_free_operations: [promise_resolve]
  predefined_globals: standard
program_premises:
  intrinsics_never_written:
    families: [array, symbol]
vendor:
  note:
    identity: replacement
    level: bundled_partial_swap
    bundle: {path: vendor.mjs}
    packages:
      note: {version: "1", subpath: "", bundle_export: lib}
    symbols:
      note: {package: note, kind: named, upstream_export: note}
chunk_export_purity:
  entry:
    source_markers: []
    pure_exports: [note]
    retained_callback_factory_members:
      namespace:
        factory:
          - {argument_count: 2, callback_arguments: [0, 1]}
chunk_analysis_options:
  entry:
    value_inputs:
      - source_match: {match: "const value = {};", bindings: [value]}
        value: {kind: ordinary_object}
        audited_source_sha256: fake-digest
        expected_export: note
    initializer_call_effect_inputs:
      - resolved_member: {module: helper, name: factory}
        audited_source_sha256: fake-digest
        provider_chunk: provider
        provider_export: factory
        audited_provider_sha256: fake-provider-digest
        effective_replacement: {package: fake-package, bundle_export: factory, symbol_kind: named, audited_bundle_sha256: fake-bundle-digest}
    native_call_identity_inputs:
      - source_match: {match: "const value = Object.freeze({});", bindings: [value]}
        callee: object_freeze
        audited_source_sha256: fake-digest
    argument_inputs:
      - source_match: {match: "function factory(value) { return value; }", bindings: [factory]}
        parameter_index: 0
        exact_arity: 1
        audited_source_sha256: fake-digest
    no_accessor_install_callables:
      - source_match: {match: "function factory() {}", bindings: [factory]}
    deferred_global_publications: {global_this: [note]}
    admission_overrides: [a1_eval]
"#,
        )
        .unwrap();
    AnalysisSpec::from_transform(&spec, Path::new(".")).unwrap()
}

use std::path::Path;

#[test]
fn projection_preserves_nested_contracts_presence_and_deterministic_identity() {
    let original = projection();
    let encoded = original.encode().unwrap();
    let restored = AnalysisSpec::decode(&encoded).unwrap();
    assert!(original.differences(&restored).unwrap().is_empty());
    assert_eq!(encoded, restored.encode().unwrap());
    assert!(
        restored.chunk_export_purity["entry"]
            .source_markers
            .as_ref()
            .unwrap()
            .is_empty()
    );
    assert!(restored.vendor.contains_key("note"));

    let mut absent = original.clone();
    absent
        .chunk_export_purity
        .get_mut("entry")
        .unwrap()
        .source_markers = None;
    assert_ne!(encoded, absent.encode().unwrap());
    assert_eq!(
        original.differences(&absent).unwrap(),
        ["chunk_export_purity.entry"]
    );

    let mut changed = restored;
    changed
        .chunk_analysis_inputs
        .get_mut("entry")
        .unwrap()
        .argument_inputs[0]
        .exact_arity = 2;
    assert_eq!(
        original.differences(&changed).unwrap(),
        ["chunk_analysis_inputs.entry"]
    );
    changed.vendor.remove("note");
    assert_eq!(
        original.differences(&changed).unwrap(),
        ["vendor.note", "chunk_analysis_inputs.entry"]
    );
}

#[test]
fn decode_rejects_missing_fields_variants_duplicate_set_members_and_truncated_data() {
    let original = projection();
    let mut message = original.to_proto().unwrap();
    message.inputs = None;
    assert!(AnalysisSpec::from_proto(message).is_err());

    let mut message = original.to_proto().unwrap();
    message.inputs.as_mut().unwrap().input_root = None;
    assert!(AnalysisSpec::from_proto(message).is_err());

    let mut message = original.to_proto().unwrap();
    message
        .host_behavior
        .as_mut()
        .unwrap()
        .synchronous_module_loader = Some(i32::MAX);
    assert!(AnalysisSpec::from_proto(message).is_err());

    let mut message = original.to_proto().unwrap();
    message
        .host_behavior
        .as_mut()
        .unwrap()
        .synchronous_module_loader = None;
    assert!(AnalysisSpec::from_proto(message).is_err());

    let mut message = original.to_proto().unwrap();
    message
        .host_behavior
        .as_mut()
        .unwrap()
        .reentry_free_builtin_modules
        .push(spec::NodeBuiltinModule::Fs as i32);
    assert!(AnalysisSpec::from_proto(message).is_err());

    assert!(AnalysisSpec::decode(&[]).is_err());
    let encoded = original.encode().unwrap();
    assert!(AnalysisSpec::decode(&encoded[..encoded.len() - 1]).is_err());
}

#[test]
fn retained_callback_signature_decode_rejects_empty_and_out_of_range_positions() {
    for callback_arguments in [vec![], vec![2], vec![1, 3]] {
        let mut original = projection();
        let signature = &mut original
            .chunk_export_purity
            .get_mut("entry")
            .unwrap()
            .retained_callback_factory_members
            .get_mut("namespace")
            .unwrap()
            .get_mut("factory")
            .unwrap()[0];
        signature.callback_arguments = callback_arguments.into_iter().collect();
        let malformed = original.encode().unwrap();
        assert!(AnalysisSpec::decode(&malformed).is_err());
    }
}

#[test]
fn identifier_modes_and_resolved_source_sites_survive_the_wire_boundary() {
    let mut original = projection();
    original
        .chunk_analysis_inputs
        .get_mut("entry")
        .unwrap()
        .value_inputs[0]
        .source_match
        .identifiers = spec::SourceMatchIdentifierMode::Exact;
    original
        .source_sites
        .source_sha256
        .insert("entry".into(), "fake-digest".into());
    original.source_sites.value_inputs.insert(
        "entry".into(),
        vec![analysis_vocabulary::inputs::SourceDeclaration {
            binding: "value".into(),
            body_index: usize::MAX,
        }],
    );
    let mut assumptions = analysis_vocabulary::inputs::AnalysisAssumptions::default();
    assumptions.known_effects.insert(
        "decorate".into(),
        analysis_vocabulary::inputs::KnownEffect::TypescriptDecorateHelper,
    );
    assumptions.no_reentry_statement_calls.insert(
        (12, 24),
        spec::RetainedCallbackSignature {
            argument_count: 2,
            callback_arguments: [1].into(),
        },
    );
    original
        .declared_assumptions
        .insert("entry".into(), assumptions);
    let restored = AnalysisSpec::decode(&original.encode().unwrap()).unwrap();
    assert_eq!(
        restored.chunk_analysis_inputs["entry"].value_inputs[0]
            .source_match
            .identifiers,
        spec::SourceMatchIdentifierMode::Exact
    );
    assert_eq!(
        restored.source_sites.value_inputs["entry"][0].body_index,
        usize::MAX
    );
    assert_eq!(
        restored.declared_assumptions["entry"].no_reentry_statement_calls[&(12, 24)]
            .callback_arguments,
        [1].into()
    );
    assert!(original.differences(&restored).unwrap().is_empty());
}

#[test]
fn analysis_rejects_non_utf8_paths_when_encoding_and_decoding() {
    use std::ffi::OsString;
    use std::os::unix::ffi::OsStringExt;
    use std::path::PathBuf;
    let path = PathBuf::from(OsString::from_vec(b"non-utf8-\xff".to_vec()));
    for field in 0..4 {
        let mut value = projection();
        match field {
            0 => value.inputs.input_root = path.clone(),
            1 => value.inputs.js_list_path = path.clone(),
            2 => value.inputs.embedded_loaders_path = Some(path.clone()),
            3 => {
                let spec::VendorLevel::BundledPartialSwap(mark) =
                    &mut value.vendor.get_mut("note").unwrap().level
                else {
                    unreachable!()
                };
                mark.bundle.path = path.clone();
            }
            _ => unreachable!(),
        }
        assert!(value.encode().is_err());
    }
    let mut message = projection().to_proto().unwrap();
    message.inputs.as_mut().unwrap().input_root = Some(b"non-utf8-\xff".to_vec());
    assert!(AnalysisSpec::from_proto(message).is_err());
    let mut message = projection().to_proto().unwrap();
    let mark = message
        .vendor
        .get_mut("note")
        .unwrap()
        .level
        .as_mut()
        .unwrap();
    let spec_wire_proto::debundle::spec::vendor_level::Value::BundledPartialSwap(mark) =
        mark.value.as_mut().unwrap()
    else {
        unreachable!()
    };
    mark.bundle.as_mut().unwrap().path = Some(b"non-utf8-\xff".to_vec());
    assert!(AnalysisSpec::from_proto(message).is_err());
}

#[test]
fn analysis_unit_enums_preserve_real_zero_and_reject_invalid_values() {
    for value in [None, Some(i32::MAX)] {
        let mut message = projection().to_proto().unwrap();
        message
            .chunk_analysis_inputs
            .get_mut("entry")
            .unwrap()
            .local_effect_policy = value;
        assert!(AnalysisSpec::from_proto(message).is_err());
    }
    {
        let mut message = projection().to_proto().unwrap();
        let mut assumptions = analysis_vocabulary::inputs::AnalysisAssumptions::default();
        assumptions.known_effects.insert(
            "decorate".into(),
            analysis_vocabulary::inputs::KnownEffect::TypescriptDecorateHelper,
        );
        let mut assumptions = encode_assumptions(&assumptions).unwrap();
        assumptions
            .known_effects
            .insert("decorate".into(), i32::MAX);
        message
            .declared_assumptions
            .insert("entry".into(), assumptions);
        assert!(AnalysisSpec::from_proto(message).is_err());
    }

    let mut message = projection().to_proto().unwrap();
    message
        .chunk_analysis_inputs
        .get_mut("entry")
        .unwrap()
        .local_effect_policy = Some(0);
    let mut assumptions = analysis_vocabulary::inputs::AnalysisAssumptions::default();
    assumptions.known_effects.insert(
        "decorate".into(),
        analysis_vocabulary::inputs::KnownEffect::TypescriptDecorateHelper,
    );
    message
        .declared_assumptions
        .insert("entry".into(), encode_assumptions(&assumptions).unwrap());
    let restored = AnalysisSpec::from_proto(message).unwrap();
    assert_eq!(
        restored.chunk_analysis_inputs["entry"].local_effect_policy,
        analysis_vocabulary::inputs::LocalEffectPolicy::KnownEffectsOnly
    );
    assert_eq!(
        restored.declared_assumptions["entry"].known_effects["decorate"],
        analysis_vocabulary::inputs::KnownEffect::TypescriptDecorateHelper
    );
    assert_eq!(
        serde_yaml::to_value(analysis_vocabulary::inputs::LocalEffectPolicy::KnownEffectsOnly)
            .unwrap(),
        serde_yaml::Value::String("known_effects_only".into())
    );
    assert_eq!(
        serde_yaml::to_value(analysis_vocabulary::inputs::KnownEffect::TypescriptDecorateHelper)
            .unwrap(),
        serde_yaml::Value::String("typescript_decorate_helper".into())
    );
}

#[test]
fn composite_keys_reject_duplicates_and_missing_coordinates() {
    let signature = spec::RetainedCallbackSignature {
        argument_count: 1,
        callback_arguments: [0].into(),
    };
    let valid = pb::StatementCallback {
        start: Some(0),
        end: Some(4),
        signature: Some(spec_wire::encode_retained_callback_signature(&signature)),
    };
    assert!(decode_statement_callbacks(vec![valid.clone(), valid.clone()]).is_err());
    assert!(
        decode_statement_callbacks(vec![pb::StatementCallback {
            start: None,
            ..valid
        }])
        .is_err()
    );
}
