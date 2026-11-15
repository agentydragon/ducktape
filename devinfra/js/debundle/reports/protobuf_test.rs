use crate::analysis_inputs::{CallbackFactoryEvidence, CallbackObjectArguments};
use crate::analysis_inputs::{NativeIntegrity, NativeIntrinsic, ValueInputRef};
use crate::reports::schema::NativeMutationOperation;
use crate::reports::schema::{
    NativeMutationWitness, NativePropertyKey, NativeReceiverPath, NativeUnreachableBranch,
    NativeWriteSlot,
};
use source_identity::SourceUnitOrdinal;
use std::collections::BTreeSet;
use std::sync::Arc;
use swc_common::{BytePos, Span};

#[test]
fn protobuf_native_census_retains_witness_and_nested_unreachable_spans() {
    let span = Span::new(BytePos(7), BytePos(12));
    let witness = NativeMutationWitness {
        chunk_id: "app".to_string(),
        statement_ordinal: SourceUnitOrdinal(3),
        source_location: None,
        operation: NativeMutationOperation::BindingAssignment,
        receiver_roots: BTreeSet::from([NativeIntrinsic::Array]),
        through_local_alias: false,
        receiver_global: None,
        affected_roots: BTreeSet::from([NativeIntrinsic::Object]),
        target_slots: BTreeSet::from([NativeWriteSlot {
            receiver: NativeReceiverPath::Prototype(NativeIntrinsic::Object),
            key: NativePropertyKey::String("name".into()),
        }]),
        key_dependencies: BTreeSet::from([NativeIntrinsic::Symbol]),
        invalidated_key_dependencies: BTreeSet::from([NativeIntrinsic::GlobalObject]),
        value_input_refs: BTreeSet::from([ValueInputRef {
            defining_chunk: "provider".into(),
            input_index: 1,
        }]),
        value_input_evidence: Vec::new(),
        unreachable_branch: Some(NativeUnreachableBranch {
            arguments: CallbackObjectArguments {
                wrapper: CallbackFactoryEvidence::AuthorAssertion {
                    defining_module: "provider".to_string(),
                    export: "wrap".to_string(),
                },
                object_arguments: BTreeSet::new(),
            },
            imported_local: "wrap".to_string(),
            wrapper_call_location: None,
            callback_location: None,
            guard_location: None,
            guard_span: Span::new(BytePos(13), BytePos(14)),
            wrapper_call_span: Span::new(BytePos(15), BytePos(16)),
            callback_span: Span::new(BytePos(17), BytePos(18)),
        }),
        span,
    };

    let json = serde_json::to_value(&witness).expect("report witness serializes");
    assert!(json.get("span").is_none());
    assert!(json["unreachable_branch"].get("guard_span").is_none());

    use crate::saved_proto::{ProtoConvert, proto};
    let expected = witness.clone();
    let census = NativeIntegrity {
        witnesses: Arc::new(vec![witness]),
        ..NativeIntegrity::default()
    };
    let wire = census.to_proto().expect("typed snapshot");
    let bytes = analysis_codec::encode_protobuf(&wire).expect("encode witness");
    let decoded: proto::NativeIntegrity =
        analysis_codec::decode_protobuf(&bytes).expect("decode witness");
    let restored = NativeIntegrity::from_proto(decoded).expect("complete census");
    let restored = &restored.witnesses[0];
    assert_eq!(restored, &expected);
    assert_eq!(restored.span, span);
    let branch = restored
        .unreachable_branch
        .as_ref()
        .expect("branch retained");
    assert_eq!(branch.guard_span, Span::new(BytePos(13), BytePos(14)));
    assert_eq!(
        branch.wrapper_call_span,
        Span::new(BytePos(15), BytePos(16))
    );
    assert_eq!(branch.callback_span, Span::new(BytePos(17), BytePos(18)));

    let mut missing_span = wire;
    let branch = missing_span.witnesses[0]
        .unreachable_branch
        .as_mut()
        .unwrap();
    branch.guard_span = None;
    assert!(NativeIntegrity::from_proto(missing_span).is_err());
}
