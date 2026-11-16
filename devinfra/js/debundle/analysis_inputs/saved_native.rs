//! The native census in saved analysis, including source spans omitted by reports.

use std::collections::BTreeSet;
use std::sync::Arc;

use anyhow::{Context, Result, ensure};
use prost::Message;
use saved_native_proto::debundle::saved_native as proto;
use saved_purity_proto::debundle::saved_purity as purity_proto;
use source_identity::SourceUnitOrdinal;

use super::{NativeIntegrity, NativeIntrinsic, ValueInputEvidence, ValueInputRef};
use crate::analysis_inputs::saved_callable::{
    decode_callback_object_arguments, encode_callback_object_arguments,
};
use crate::purity::saved::dependencies::{decode_host_behavior, encode_host_behavior};
use crate::purity::saved::{
    decode_source_location, decode_span, encode_source_location, encode_span,
};
use crate::reports::schema::{
    NativeMutationOperation, NativeMutationWitness, NativePropertyKey, NativePrototypeMutation,
    NativePrototypeMutationKind, NativeReceiverPath, NativeUnreachableBranch, NativeWriteSlot,
    NeverWrittenOverride, NeverWrittenReach,
};

pub fn encode(integrity: &NativeIntegrity) -> Result<Vec<u8>> {
    // The protobuf stores the stable reference in its evidence record once.
    for (reference, evidence) in &integrity.value_input_evidence {
        ensure!(
            reference == &evidence.reference,
            "native evidence key differs from its reference"
        );
    }
    Ok(proto::NativeIntegrity {
        mutated: encode_intrinsics(&integrity.mutated),
        prototype_mutations: integrity
            .prototype_mutations
            .iter()
            .map(encode_prototype)
            .collect(),
        witnesses: integrity.witnesses.iter().map(encode_witness).collect(),
        value_input_evidence: integrity
            .value_input_evidence
            .values()
            .map(encode_value_evidence)
            .collect(),
        consumed_value_inputs: integrity
            .consumed_value_inputs
            .iter()
            .map(encode_value_ref)
            .collect(),
        consumed_host_behavior: integrity
            .consumed_host_behavior
            .iter()
            .map(encode_host_behavior)
            .collect(),
        never_written: encode_intrinsics(&integrity.never_written),
        never_written_overrides: integrity
            .never_written_overrides
            .iter()
            .map(|item| proto::NeverWrittenOverride {
                witness: item.witness as u64,
                chunk_id: item.chunk_id.clone(),
                source_location: item.source_location.as_ref().map(encode_source_location),
                families: encode_intrinsics(&item.families),
                reach: Some(item.reach as i32),
            })
            .collect(),
    }
    .encode_to_vec())
}

pub fn decode(bytes: &[u8]) -> Result<NativeIntegrity> {
    let saved = proto::NativeIntegrity::decode(bytes)?;
    let mut evidence = std::collections::BTreeMap::new();
    for item in saved.value_input_evidence {
        let item = decode_value_evidence(item)?;
        ensure!(
            evidence.insert(item.reference.clone(), item).is_none(),
            "duplicate native value-input evidence"
        );
    }
    Ok(NativeIntegrity {
        mutated: decode_intrinsics(saved.mutated)?,
        prototype_mutations: Arc::new(
            saved
                .prototype_mutations
                .into_iter()
                .map(decode_prototype)
                .collect::<Result<_>>()?,
        ),
        witnesses: Arc::new(
            saved
                .witnesses
                .into_iter()
                .map(decode_witness)
                .collect::<Result<_>>()?,
        ),
        value_input_evidence: evidence,
        consumed_value_inputs: saved
            .consumed_value_inputs
            .into_iter()
            .map(decode_value_ref)
            .collect::<Result<_>>()?,
        consumed_host_behavior: saved
            .consumed_host_behavior
            .into_iter()
            .map(decode_host_behavior)
            .collect::<Result<_>>()?,
        never_written: decode_intrinsics(saved.never_written)?,
        never_written_overrides: saved
            .never_written_overrides
            .into_iter()
            .map(|item| {
                Ok(NeverWrittenOverride {
                    witness: item.witness.try_into()?,
                    chunk_id: item.chunk_id,
                    source_location: item
                        .source_location
                        .map(decode_source_location)
                        .transpose()?,
                    families: decode_intrinsics(item.families)?,
                    reach: NeverWrittenReach::try_from(
                        item.reach.context("missing saved override reach")?,
                    )?,
                })
            })
            .collect::<Result<_>>()?,
    })
}

fn encode_intrinsics(intrinsics: &BTreeSet<NativeIntrinsic>) -> Vec<i32> {
    intrinsics
        .iter()
        .map(|intrinsic| *intrinsic as i32)
        .collect()
}

fn decode_intrinsics(intrinsics: Vec<i32>) -> Result<BTreeSet<NativeIntrinsic>> {
    intrinsics
        .into_iter()
        .map(|intrinsic| Ok(NativeIntrinsic::try_from(intrinsic)?))
        .collect()
}

fn encode_value_ref(reference: &ValueInputRef) -> purity_proto::AuthorInputRef {
    purity_proto::AuthorInputRef {
        defining_chunk: reference.defining_chunk.clone(),
        input_index: reference.input_index as u64,
    }
}

fn decode_value_ref(reference: purity_proto::AuthorInputRef) -> Result<ValueInputRef> {
    Ok(ValueInputRef {
        defining_chunk: reference.defining_chunk,
        input_index: reference.input_index.try_into()?,
    })
}

pub(crate) fn encode_value_evidence(evidence: &ValueInputEvidence) -> proto::ValueInputEvidence {
    proto::ValueInputEvidence {
        reference: Some(encode_value_ref(&evidence.reference)),
        spec_location: evidence.spec_location.clone(),
        source_match: Some(spec_wire::encode_source_match_claim(&evidence.source_match)),
        value_kind: Some(evidence.value_kind as i32),
        source_path: evidence.source_path.clone(),
        parser_input_file: evidence.parser_input_file.clone(),
        audited_source_sha256: evidence.audited_source_sha256.clone(),
        selected_binding: evidence.selected_binding.clone(),
        declaration_location: evidence
            .declaration_location
            .as_ref()
            .map(encode_source_location),
        initializer_location: evidence
            .initializer_location
            .as_ref()
            .map(encode_source_location),
        expected_export: evidence.expected_export.clone(),
        note: evidence.note.clone(),
    }
}

pub(crate) fn decode_value_evidence(
    evidence: proto::ValueInputEvidence,
) -> Result<ValueInputEvidence> {
    Ok(ValueInputEvidence {
        reference: decode_value_ref(
            evidence
                .reference
                .context("missing saved value-input reference")?,
        )?,
        spec_location: evidence.spec_location,
        source_match: spec_wire::decode_source_match_claim(
            evidence
                .source_match
                .context("missing saved value-input source match")?,
        )?,
        value_kind: spec::CapturedValueKind::try_from(
            evidence
                .value_kind
                .context("missing saved captured-value kind")?,
        )?,
        source_path: evidence.source_path,
        parser_input_file: evidence.parser_input_file,
        audited_source_sha256: evidence.audited_source_sha256,
        selected_binding: evidence.selected_binding,
        declaration_location: evidence
            .declaration_location
            .map(decode_source_location)
            .transpose()?,
        initializer_location: evidence
            .initializer_location
            .map(decode_source_location)
            .transpose()?,
        expected_export: evidence.expected_export,
        note: evidence.note,
    })
}

fn encode_prototype(mutation: &NativePrototypeMutation) -> proto::PrototypeMutation {
    proto::PrototypeMutation {
        chunk_id: mutation.chunk_id.clone(),
        statement_ordinal: mutation.statement_ordinal.0 as u64,
        source_location: mutation
            .source_location
            .as_ref()
            .map(encode_source_location),
        kind: Some(mutation.kind as i32),
        properties: mutation
            .properties
            .as_ref()
            .map(|names| proto::PropertyNames {
                names: names.iter().cloned().collect(),
            }),
        key_dependencies: encode_intrinsics(&mutation.key_dependencies),
        span: Some(encode_span(mutation.span)),
    }
}

fn decode_prototype(mutation: proto::PrototypeMutation) -> Result<NativePrototypeMutation> {
    Ok(NativePrototypeMutation {
        chunk_id: mutation.chunk_id,
        statement_ordinal: SourceUnitOrdinal(mutation.statement_ordinal.try_into()?),
        source_location: mutation
            .source_location
            .map(decode_source_location)
            .transpose()?,
        kind: NativePrototypeMutationKind::try_from(
            mutation
                .kind
                .context("missing saved prototype mutation kind")?,
        )?,
        properties: mutation
            .properties
            .map(|names| names.names.into_iter().collect()),
        key_dependencies: decode_intrinsics(mutation.key_dependencies)?,
        span: decode_span(
            mutation
                .span
                .context("missing saved prototype mutation span")?,
        )?,
    })
}

fn encode_witness(witness: &NativeMutationWitness) -> proto::MutationWitness {
    proto::MutationWitness {
        chunk_id: witness.chunk_id.clone(),
        statement_ordinal: witness.statement_ordinal.0 as u64,
        source_location: witness.source_location.as_ref().map(encode_source_location),
        operation: Some(witness.operation as i32),
        receiver_roots: encode_intrinsics(&witness.receiver_roots),
        through_local_alias: witness.through_local_alias,
        receiver_global: witness.receiver_global.clone(),
        affected_roots: encode_intrinsics(&witness.affected_roots),
        target_slots: witness.target_slots.iter().map(encode_slot).collect(),
        key_dependencies: encode_intrinsics(&witness.key_dependencies),
        invalidated_key_dependencies: encode_intrinsics(&witness.invalidated_key_dependencies),
        value_input_refs: witness
            .value_input_refs
            .iter()
            .map(encode_value_ref)
            .collect(),
        value_input_evidence: witness
            .value_input_evidence
            .iter()
            .map(encode_value_evidence)
            .collect(),
        unreachable_branch: witness.unreachable_branch.as_ref().map(|branch| {
            proto::UnreachableBranch {
                arguments: Some(encode_callback_object_arguments(&branch.arguments)),
                imported_local: branch.imported_local.clone(),
                wrapper_call_location: branch
                    .wrapper_call_location
                    .as_ref()
                    .map(encode_source_location),
                callback_location: branch
                    .callback_location
                    .as_ref()
                    .map(encode_source_location),
                guard_location: branch.guard_location.as_ref().map(encode_source_location),
                guard_span: Some(encode_span(branch.guard_span)),
                wrapper_call_span: Some(encode_span(branch.wrapper_call_span)),
                callback_span: Some(encode_span(branch.callback_span)),
            }
        }),
        span: Some(encode_span(witness.span)),
    }
}

fn decode_witness(witness: proto::MutationWitness) -> Result<NativeMutationWitness> {
    Ok(NativeMutationWitness {
        chunk_id: witness.chunk_id,
        statement_ordinal: SourceUnitOrdinal(witness.statement_ordinal.try_into()?),
        source_location: witness
            .source_location
            .map(decode_source_location)
            .transpose()?,
        operation: NativeMutationOperation::try_from(
            witness
                .operation
                .context("missing saved native mutation operation")?,
        )?,
        receiver_roots: decode_intrinsics(witness.receiver_roots)?,
        through_local_alias: witness.through_local_alias,
        receiver_global: witness.receiver_global,
        affected_roots: decode_intrinsics(witness.affected_roots)?,
        target_slots: witness
            .target_slots
            .into_iter()
            .map(decode_slot)
            .collect::<Result<_>>()?,
        key_dependencies: decode_intrinsics(witness.key_dependencies)?,
        invalidated_key_dependencies: decode_intrinsics(witness.invalidated_key_dependencies)?,
        value_input_refs: witness
            .value_input_refs
            .into_iter()
            .map(decode_value_ref)
            .collect::<Result<_>>()?,
        value_input_evidence: witness
            .value_input_evidence
            .into_iter()
            .map(decode_value_evidence)
            .collect::<Result<_>>()?,
        unreachable_branch: witness
            .unreachable_branch
            .map(|branch| -> Result<_> {
                Ok(NativeUnreachableBranch {
                    arguments: decode_callback_object_arguments(
                        branch
                            .arguments
                            .context("missing saved unreachable-branch arguments")?,
                    )?,
                    imported_local: branch.imported_local,
                    wrapper_call_location: branch
                        .wrapper_call_location
                        .map(decode_source_location)
                        .transpose()?,
                    callback_location: branch
                        .callback_location
                        .map(decode_source_location)
                        .transpose()?,
                    guard_location: branch
                        .guard_location
                        .map(decode_source_location)
                        .transpose()?,
                    guard_span: decode_span(
                        branch.guard_span.context("missing saved guard span")?,
                    )?,
                    wrapper_call_span: decode_span(
                        branch
                            .wrapper_call_span
                            .context("missing saved wrapper call span")?,
                    )?,
                    callback_span: decode_span(
                        branch
                            .callback_span
                            .context("missing saved callback span")?,
                    )?,
                })
            })
            .transpose()?,
        span: decode_span(witness.span.context("missing saved native mutation span")?)?,
    })
}

fn encode_slot(slot: &NativeWriteSlot) -> proto::WriteSlot {
    // Both nested protobuf vocabularies name their unit-case enum `Special`.
    use proto::property_key::{Affixed, Key, Special as KeySpecial};
    use proto::receiver_path::{Path, Special};
    proto::WriteSlot {
        receiver: Some(proto::ReceiverPath {
            path: Some(match &slot.receiver {
                NativeReceiverPath::GlobalObject => Path::Special(Special::GlobalObject as i32),
                NativeReceiverPath::Constructor(family) => Path::Constructor(*family as i32),
                NativeReceiverPath::Prototype(family) => Path::Prototype(*family as i32),
                NativeReceiverPath::Descendant(family) => Path::Descendant(*family as i32),
                NativeReceiverPath::ArrayIteratorPrototype => {
                    Path::Special(Special::ArrayIteratorPrototype as i32)
                }
                NativeReceiverPath::SetIteratorPrototype => {
                    Path::Special(Special::SetIteratorPrototype as i32)
                }
                NativeReceiverPath::Unknown => Path::Special(Special::Unknown as i32),
            }),
        }),
        key: Some(proto::PropertyKey {
            key: Some(match &slot.key {
                NativePropertyKey::String(key) => Key::StringKey(key.clone()),
                NativePropertyKey::Affixed { prefix, suffix } => Key::Affixed(Affixed {
                    prefix: prefix.clone(),
                    suffix: suffix.clone(),
                }),
                NativePropertyKey::SymbolIterator => {
                    Key::Special(KeySpecial::SymbolIterator as i32)
                }
                NativePropertyKey::OtherSymbol => Key::Special(KeySpecial::OtherSymbol as i32),
                NativePropertyKey::Unknown => Key::Special(KeySpecial::Unknown as i32),
            }),
        }),
    }
}

fn decode_slot(slot: proto::WriteSlot) -> Result<NativeWriteSlot> {
    // Both nested protobuf vocabularies name their unit-case enum `Special`.
    use proto::property_key::{Key, Special as KeySpecial};
    use proto::receiver_path::{Path, Special};
    Ok(NativeWriteSlot {
        receiver: match slot
            .receiver
            .and_then(|receiver| receiver.path)
            .context("missing saved native receiver path")?
        {
            Path::Constructor(family) => {
                NativeReceiverPath::Constructor(NativeIntrinsic::try_from(family)?)
            }
            Path::Prototype(family) => {
                NativeReceiverPath::Prototype(NativeIntrinsic::try_from(family)?)
            }
            Path::Descendant(family) => {
                NativeReceiverPath::Descendant(NativeIntrinsic::try_from(family)?)
            }
            Path::Special(value) => match Special::try_from(value)? {
                Special::GlobalObject => NativeReceiverPath::GlobalObject,
                Special::ArrayIteratorPrototype => NativeReceiverPath::ArrayIteratorPrototype,
                Special::SetIteratorPrototype => NativeReceiverPath::SetIteratorPrototype,
                Special::Unknown => NativeReceiverPath::Unknown,
            },
        },
        key: match slot
            .key
            .and_then(|key| key.key)
            .context("missing saved native property key")?
        {
            Key::StringKey(key) => NativePropertyKey::String(key),
            Key::Affixed(affixed) => NativePropertyKey::Affixed {
                prefix: affixed.prefix,
                suffix: affixed.suffix,
            },
            Key::Special(value) => match KeySpecial::try_from(value)? {
                KeySpecial::SymbolIterator => NativePropertyKey::SymbolIterator,
                KeySpecial::OtherSymbol => NativePropertyKey::OtherSymbol,
                KeySpecial::Unknown => NativePropertyKey::Unknown,
            },
        },
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analysis_inputs::{
        CallbackFactoryEvidence, CallbackObjectArguments, HostBehaviorKey,
    };
    use swc_common::{BytePos, Span};

    #[test]
    fn protobuf_census_retains_artifact_spans_and_property_presence() {
        let span = Span::new(BytePos(7), BytePos(12));
        let witness = NativeMutationWitness {
            chunk_id: "app".to_string(),
            statement_ordinal: SourceUnitOrdinal(3),
            source_location: None,
            operation: NativeMutationOperation::BindingAssignment,
            receiver_roots: BTreeSet::from([NativeIntrinsic::GlobalObject]),
            through_local_alias: true,
            receiver_global: Some(String::new()),
            affected_roots: BTreeSet::from([NativeIntrinsic::Object]),
            target_slots: BTreeSet::from([NativeWriteSlot {
                receiver: NativeReceiverPath::Prototype(NativeIntrinsic::Object),
                key: NativePropertyKey::Affixed {
                    prefix: "on".into(),
                    suffix: "Changed".into(),
                },
            }]),
            key_dependencies: BTreeSet::from([NativeIntrinsic::Symbol]),
            invalidated_key_dependencies: BTreeSet::new(),
            value_input_refs: BTreeSet::new(),
            value_input_evidence: Vec::new(),
            unreachable_branch: Some(NativeUnreachableBranch {
                arguments: CallbackObjectArguments {
                    wrapper: CallbackFactoryEvidence::AuthorAssertion {
                        defining_module: "provider".to_string(),
                        export: "wrap".to_string(),
                    },
                    object_arguments: BTreeSet::from([0]),
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
        let prototype_mutations = [
            None,
            Some(BTreeSet::new()),
            Some(BTreeSet::from(["name".into()])),
        ]
        .into_iter()
        .map(|properties| NativePrototypeMutation {
            chunk_id: "app".into(),
            statement_ordinal: SourceUnitOrdinal(3),
            source_location: None,
            kind: NativePrototypeMutationKind::Write,
            properties,
            key_dependencies: BTreeSet::new(),
            span,
        })
        .collect();
        let integrity = NativeIntegrity {
            mutated: witness.affected_roots.clone(),
            witnesses: Arc::new(vec![witness]),
            prototype_mutations: Arc::new(prototype_mutations),
            never_written: BTreeSet::from([NativeIntrinsic::Array]),
            never_written_overrides: vec![NeverWrittenOverride {
                witness: 0,
                chunk_id: "app".into(),
                source_location: None,
                families: BTreeSet::from([NativeIntrinsic::Array]),
                reach: NeverWrittenReach::Family,
            }],
            ..NativeIntegrity::default()
        };
        let reference = ValueInputRef {
            defining_chunk: "provider".into(),
            input_index: 2,
        };
        let evidence = ValueInputEvidence {
            reference: reference.clone(),
            spec_location: "value_inputs[2]".into(),
            source_match: spec::SourceMatchClaim {
                identifiers: spec::SourceMatchIdentifierMode::Exact,
                match_source: "const captured = window.settings;".into(),
                bindings: vec![],
                note: None,
            },
            value_kind: spec::CapturedValueKind::OrdinaryObject,
            source_path: "provider.js".into(),
            parser_input_file: None,
            audited_source_sha256: "digest".into(),
            selected_binding: "captured".into(),
            declaration_location: None,
            initializer_location: None,
            expected_export: Some(String::new()),
            note: Some(String::new()),
        };
        let mut integrity = integrity;
        integrity
            .value_input_evidence
            .insert(reference.clone(), evidence.clone());
        integrity.consumed_value_inputs.insert(reference.clone());
        integrity
            .consumed_host_behavior
            .insert(HostBehaviorKey::PredefinedGlobals);
        Arc::make_mut(&mut integrity.witnesses)[0]
            .value_input_refs
            .insert(reference);
        Arc::make_mut(&mut integrity.witnesses)[0]
            .value_input_evidence
            .push(evidence);
        let bytes = encode(&integrity).unwrap();
        let restored = decode(&bytes).unwrap();
        assert_eq!(restored.witnesses, integrity.witnesses);
        assert_eq!(restored.prototype_mutations, integrity.prototype_mutations);
        assert_eq!(restored.never_written, integrity.never_written);
        assert_eq!(
            restored.never_written_overrides,
            integrity.never_written_overrides
        );
        assert_eq!(restored.mutated, integrity.mutated);
        assert_eq!(
            restored.value_input_evidence,
            integrity.value_input_evidence
        );
        assert_eq!(
            restored.consumed_value_inputs,
            integrity.consumed_value_inputs
        );
        assert_eq!(
            restored.consumed_host_behavior,
            integrity.consumed_host_behavior
        );

        // Span metadata is trusted by later analysis, but intentionally absent
        // from ordinary reports. A report round-trip cannot replace this path.
        let report = serde_json::to_value(&restored.witnesses[0]).unwrap();
        assert!(report.get("span").is_none());
        assert!(report["unreachable_branch"].get("guard_span").is_none());
        let mut duplicate = proto::NativeIntegrity::decode(bytes.as_slice()).unwrap();
        duplicate
            .value_input_evidence
            .push(duplicate.value_input_evidence[0].clone());
        assert!(decode(&duplicate.encode_to_vec()).is_err());
        let evidence = integrity
            .value_input_evidence
            .values()
            .next()
            .unwrap()
            .clone();
        integrity.value_input_evidence.clear();
        integrity.value_input_evidence.insert(
            ValueInputRef {
                defining_chunk: "different_provider".into(),
                input_index: 0,
            },
            evidence,
        );
        assert!(encode(&integrity).is_err());

        let mut incomplete = proto::NativeIntegrity::decode(bytes.as_slice()).unwrap();
        incomplete.witnesses[0]
            .unreachable_branch
            .as_mut()
            .unwrap()
            .guard_span = None;
        assert!(decode(&incomplete.encode_to_vec()).is_err());
    }

    #[test]
    fn native_slot_sum_types_retain_every_receiver_and_key_shape() {
        let receivers = [
            NativeReceiverPath::GlobalObject,
            NativeReceiverPath::Constructor(NativeIntrinsic::Array),
            NativeReceiverPath::Prototype(NativeIntrinsic::Object),
            NativeReceiverPath::Descendant(NativeIntrinsic::UnknownGlobalValue),
            NativeReceiverPath::ArrayIteratorPrototype,
            NativeReceiverPath::SetIteratorPrototype,
            NativeReceiverPath::Unknown,
        ];
        let keys = [
            NativePropertyKey::String(String::new()),
            NativePropertyKey::Affixed {
                prefix: "on".into(),
                suffix: String::new(),
            },
            NativePropertyKey::SymbolIterator,
            NativePropertyKey::OtherSymbol,
            NativePropertyKey::Unknown,
        ];
        for receiver in receivers {
            for key in &keys {
                let slot = NativeWriteSlot {
                    receiver: receiver.clone(),
                    key: key.clone(),
                };
                assert_eq!(decode_slot(encode_slot(&slot)).unwrap(), slot);
            }
        }
        assert!(decode_slot(proto::WriteSlot::default()).is_err());
    }
}
