//! Namespace-family closure and fresh-error write certification.

use super::fresh_values::{FreshValueStep, fresh_value_writes};
use super::intrinsic_roots::IntrinsicRoots;
use super::{
    CallKind, CallbackResultWrite, ExecutionIndex, FreshErrorWriteSite,
    infer_mutation_receiver_aliases,
};
use analysis::facts::statement_facts::{
    CallbackFactoryCallArgumentWitness, CallbackFactoryWriteWitness,
};
use analysis_vocabulary::inputs::NativeIntrinsic;
use inference::{FactContext, IterativeInference, IterativeRules};
use source_identity::SourceUnitOrdinal;
use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};
use swc_ecma_ast::*;
use value_flow::ChunkValueFlow;
use value_model::{NamespaceValueFacts, ValueExpr, infer_namespace_aliases};

impl ExecutionIndex {
    /// Fresh-error write sites whose every write is a certified claim.
    pub(super) fn certified_fresh_error_sites(
        &self,
        flow: Option<&ChunkValueFlow>,
        mutated_intrinsics: &IntrinsicRoots,
    ) -> BTreeMap<FreshErrorWriteSite, usize> {
        let mut claim_counts = BTreeMap::<FreshErrorWriteSite, usize>::new();
        if !self.fresh_error_claims.is_empty()
            && !self.audited_factory_members.is_empty()
            && flow.is_some()
        {
            // A11 supplies the standard Error allocation/prototype behavior. Reject
            // visible replacement of that intrinsic, including writes through local
            // aliases of a known global object. This is not a general effect proof.
            let intrinsic_mutated = mutated_intrinsics.contains(&NativeIntrinsic::Error)
                || mutated_intrinsics.contains(&NativeIntrinsic::GlobalObject);
            if !intrinsic_mutated
                && self.dynamic_scope_owners.is_empty()
                && let Some(flow) = flow
            {
                for claim in &self.fresh_error_claims {
                    if !flow.is_declared(&claim.callee)
                        && claim
                            .local_read
                            .as_ref()
                            .is_none_or(|id| flow.is_declared(id))
                    {
                        *claim_counts.entry(claim.site.clone()).or_default() += 1;
                    }
                }
            }
            let mut write_counts = BTreeMap::<FreshErrorWriteSite, usize>::new();
            let mut count_writes = |owner, callable, writes: &[CallbackResultWrite]| {
                for write in writes {
                    if let (ValueExpr::Binding(receiver), Some(property)) =
                        (&write.receiver, &write.property)
                    {
                        *write_counts
                            .entry(FreshErrorWriteSite {
                                owner,
                                callable,
                                receiver: receiver.clone(),
                                property: property.clone(),
                            })
                            .or_default() += 1;
                    }
                }
            };
            for (owner, writes) in self.top_callback_result_writes.iter().enumerate() {
                count_writes(SourceUnitOrdinal(owner), None, writes);
            }
            for (key, callable) in &self.callables {
                count_writes(callable.owner, Some(*key), &callable.callback_result_writes);
            }
            claim_counts.retain(|site, count| write_counts.get(site) == Some(count));
        }
        claim_counts
    }

    /// Rejects audited factory, no-reentry and record members until closure and
    /// keeps the rejection witnesses.
    pub(super) fn close_namespace_family(
        &mut self,
        namespace_assignments: &[&(Id, ValueExpr)],
        namespace_value_facts: &NamespaceValueFacts,
        fresh_steps: &[Option<FreshValueStep>],
        claim_counts: &BTreeMap<FreshErrorWriteSite, usize>,
    ) {
        let certified = |owner, callable, receiver: &Id, property: &str| {
            claim_counts.contains_key(&FreshErrorWriteSite {
                owner,
                callable,
                receiver: receiver.clone(),
                property: property.to_owned(),
            })
        };
        let certified_callback_write = |owner, callable, write: &CallbackResultWrite| matches!((&write.receiver, &write.property), (ValueExpr::Binding(receiver), Some(property)) if certified(owner, callable, receiver, property));
        // A namespace member is an audited target only while its binding is
        // unchanged. Any local write could replace a member with an eager
        // callback invoker before the apparent factory call.
        // Total source privacy proves that this family cannot reach any unrelated
        // Unknown/This slot. It grants no execution or callback timing effects.
        let private_namespace_family = !self.namespace_privacy.members.is_empty()
            && !self.namespace_privacy.invalid
            && self.dynamic_scope_owners.is_empty();
        let namespace_member_write_witnesses =
            RefCell::new(BTreeMap::<(Id, String), CallbackFactoryWriteWitness>::new());
        let namespace_dynamic_property_write_witnesses =
            RefCell::new(BTreeMap::<(Id, String), CallbackFactoryWriteWitness>::new());
        let namespace_call_argument_witnesses =
            RefCell::new(BTreeMap::<Id, CallbackFactoryCallArgumentWitness>::new());
        let keep_namespace = |id: &Id, members: &BTreeSet<String>, writes: &BTreeSet<_>| {
            if private_namespace_family {
                return true;
            }
            let aliases = infer_namespace_aliases(id, namespace_assignments, namespace_value_facts);
            let receiver_aliases = infer_mutation_receiver_aliases(
                id,
                namespace_assignments,
                &aliases,
                namespace_value_facts,
            );
            let is_namespace_or_alias = |binding: &Id| binding == id || aliases.contains(binding);
            let is_mutation_receiver = |receiver: &ValueExpr| {
                receiver.may_be_namespace_mutation_receiver(
                    id,
                    &aliases,
                    &receiver_aliases,
                    namespace_value_facts,
                )
            };
            // Keep the original conservative admission predicate independent
            // of diagnostic witness extraction below.
            let top_member_write_rejected = self
                .top_property_writes
                .iter()
                .enumerate()
                .filter(|(owner, _)| !writes.contains(&SourceUnitOrdinal(*owner)))
                .flat_map(|(owner, writes)| {
                    writes
                        .iter()
                        .map(move |write| (SourceUnitOrdinal(owner), write))
                })
                .any(|(owner, (receiver, property))| {
                    receiver.as_ref().is_some_and(|receiver| {
                        !property
                            .as_ref()
                            .is_some_and(|property| certified(owner, None, receiver, property))
                            && ((receiver == id
                                && property
                                    .as_ref()
                                    .is_none_or(|property| members.contains(property)))
                                || aliases.contains(receiver)
                                || receiver_aliases.contains(receiver))
                    })
                });
            let top_member_write_witness = self
                .top_property_writes
                .iter()
                .enumerate()
                .filter(|(owner, _)| !writes.contains(&SourceUnitOrdinal(*owner)))
                .flat_map(|(owner, writes)| {
                    writes
                        .iter()
                        .map(move |write| (SourceUnitOrdinal(owner), write))
                })
                .find_map(|(owner, (receiver, property))| {
                    let receiver = receiver.as_ref()?;
                    let property = property.as_ref()?;
                    (members.contains(property)
                        && !certified(owner, None, receiver, property)
                        && (receiver == id
                            || aliases.contains(receiver)
                            || receiver_aliases.contains(receiver)))
                    .then_some((owner, receiver, property))
                });
            if let Some((statement_ordinal, receiver_binding, property)) = &top_member_write_witness
            {
                let witness = CallbackFactoryWriteWitness {
                    statement_ordinal: *statement_ordinal,
                    receiver_binding: receiver_binding.0.to_string(),
                    property: Some((*property).clone()),
                };
                let mut recorded = namespace_member_write_witnesses.borrow_mut();
                for member in members {
                    recorded
                        .entry((id.clone(), member.clone()))
                        .or_insert_with(|| witness.clone());
                }
            }
            let destructuring_write = self.destructuring_member_writes.iter().any(|write| {
                is_mutation_receiver(&write.receiver)
                    || write
                        .source
                        .may_publish_namespace(id, &aliases, namespace_value_facts)
            });
            let top_dynamic_write = self
                .top_dynamic_property_receivers
                .iter()
                .flatten()
                .any(is_mutation_receiver);
            // This is a diagnostic for the already rejected guard, not an
            // admission rule. Only a concrete binding receiver supplies an
            // attributable source witness; opaque receiver shapes stay unknown.
            let dynamic_write_witness = self
                .top_property_writes
                .iter()
                .enumerate()
                .filter(|(owner, _)| !writes.contains(&SourceUnitOrdinal(*owner)))
                .flat_map(|(owner, writes)| writes.iter().map(move |write| (owner, write)))
                .find_map(|(owner, (receiver, property))| match (receiver, property) {
                    (Some(binding), None)
                        if binding == id
                            || aliases.contains(binding)
                            || receiver_aliases.contains(binding) =>
                    {
                        Some(CallbackFactoryWriteWitness {
                            statement_ordinal: SourceUnitOrdinal(owner),
                            receiver_binding: binding.0.to_string(),
                            property: None,
                        })
                    }
                    _ => None,
                });
            if let Some(witness) = dynamic_write_witness {
                let mut recorded = namespace_dynamic_property_write_witnesses.borrow_mut();
                for member in members {
                    recorded
                        .entry((id.clone(), member.clone()))
                        .or_insert_with(|| witness.clone());
                }
            }
            let top_callback_result_write = self
                .top_callback_result_writes
                .iter()
                .enumerate()
                .filter(|(owner, _)| !writes.contains(&SourceUnitOrdinal(*owner)))
                .flat_map(|(owner, writes)| {
                    writes
                        .iter()
                        .map(move |write| (SourceUnitOrdinal(owner), write))
                })
                .any(|(owner, write)| {
                    (is_mutation_receiver(&write.receiver)
                        && !certified_callback_write(owner, None, write))
                        || write
                            .value
                            .may_publish_namespace(id, &aliases, namespace_value_facts)
                });
            let callable_write = self.callables.iter().any(|(key, callable)| {
                callable.member_writes.iter().any(|(receiver, property)| {
                    !certified(callable.owner, Some(*key), receiver, property)
                        && ((receiver == id && members.contains(property))
                            || aliases.contains(receiver)
                            || receiver_aliases.contains(receiver))
                }) || callable
                    .dynamic_property_receivers
                    .iter()
                    .any(is_mutation_receiver)
                    || callable.literal_exposures.iter().any(is_namespace_or_alias)
                    || callable.escaped_bindings.iter().any(is_namespace_or_alias)
                    || callable.namespace_escape_values.iter().any(|value| {
                        value.may_publish_namespace(id, &aliases, namespace_value_facts)
                    })
                    || callable.member_value_writes.iter().any(|write| {
                        write
                            .value
                            .may_publish_namespace(id, &aliases, namespace_value_facts)
                    })
                    || callable.callback_result_writes.iter().any(|write| {
                        (is_mutation_receiver(&write.receiver)
                            && !certified_callback_write(callable.owner, Some(*key), write))
                            || write.value.may_publish_namespace(
                                id,
                                &aliases,
                                namespace_value_facts,
                            )
                    })
                    || callable
                        .body_facts
                        .value_flow()
                        .returns()
                        .iter()
                        .any(|value| {
                            value.may_publish_namespace_in_factory_result(
                                id,
                                &aliases,
                                namespace_value_facts,
                            )
                        })
                    || callable.calls.iter().any(|call| {
                        call.args.iter().any(|value| {
                            value.may_publish_namespace(id, &aliases, namespace_value_facts)
                        })
                    })
            });
            let top_member_value = self.top_member_value_writes.iter().flatten().any(|write| {
                write
                    .value
                    .may_publish_namespace(id, &aliases, namespace_value_facts)
            });
            let top_call_argument = self.top_calls.iter().flatten().any(|call| {
                call.args
                    .iter()
                    .any(|value| value.may_publish_namespace(id, &aliases, namespace_value_facts))
            });
            if top_call_argument {
                let direct = self
                    .top_calls
                    .iter()
                    .flatten()
                    .filter(|call| {
                        call.kind == CallKind::Call
                            && call.caller.is_none()
                            && !call.has_spread
                            && self.ordinary_top_call_sites.contains(&call.span.into())
                    })
                    .flat_map(|call| {
                        call.args
                                .iter()
                                .enumerate()
                                .filter(|(_, value)| {
                                    matches!(*value, ValueExpr::Binding(binding) if binding == id)
                                })
                                .map(move |(argument_index, _)| CallbackFactoryCallArgumentWitness {
                                    statement_ordinal: call.owner,
                                    call_span: call.span,
                                    argument_index,
                                    namespace_binding: id.0.to_string(),
                                })
                    })
                    .min_by_key(|witness| {
                        (
                            witness.statement_ordinal,
                            witness.call_span.lo.0,
                            witness.argument_index,
                        )
                    });
                if let Some(witness) = direct {
                    namespace_call_argument_witnesses
                        .borrow_mut()
                        .insert(id.clone(), witness);
                }
            }
            let exported_value = self
                .top_exported_values
                .iter()
                .flatten()
                .any(|value| value.may_publish_namespace(id, &aliases, namespace_value_facts))
                || self.exported_bindings.iter().any(is_namespace_or_alias);
            !(top_member_write_rejected
                || destructuring_write
                || top_dynamic_write
                || top_callback_result_write
                || callable_write
                || top_member_value
                || top_call_argument
                || exported_value)
        };
        let namespace_family: Vec<_> = self.namespace_privacy.members.keys().cloned().collect();
        let family_audited_members: BTreeSet<_> = if namespace_family.is_empty() {
            BTreeSet::new()
        } else {
            self.audited_factory_members
                .values()
                .chain(self.no_reentry_members.values())
                .chain(self.no_reentry_record_members.values())
                .flat_map(|members| members.iter().cloned())
                .collect()
        };
        let factory_imports = self
            .callback_factory_imports
            .union(&self.inline_callback_factory_imports)
            .cloned()
            .collect();
        // Candidate rejection grows to closure. No provisional admission is
        // exported: removing a factory can invalidate fresh-result writes used
        // by another candidate in the next evaluation.
        type Members = BTreeMap<Id, BTreeSet<String>>;
        #[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
        enum Rejected {
            Factory(Id),
            NoReentry(Id),
            NoReentryRecord(Id),
        }
        struct NamespaceRules<'a, Fresh, Keep> {
            factories: &'a Members,
            legacy: &'a Members,
            no_reentry: &'a Members,
            records: &'a Members,
            independent: &'a Members,
            fresh: &'a Fresh,
            keep: &'a Keep,
        }
        impl<Fresh, Keep> IterativeRules for NamespaceRules<'_, Fresh, Keep>
        where
            Fresh: Fn(&Members) -> (BTreeSet<SourceUnitOrdinal>, BTreeSet<SourceUnitOrdinal>),
            Keep: Fn(&Id, &BTreeSet<String>, &BTreeSet<SourceUnitOrdinal>) -> bool,
        {
            type Key = Rejected;
            type Fact = ();
            type Rule = ();
            type MergeScratch = ();
            fn evaluate(&self, _: &(), context: &mut impl FactContext<Self::Key, Self::Fact>) {
                let legacy: Members = self
                    .legacy
                    .iter()
                    .filter(|(id, _)| context.read(Rejected::Factory((*id).clone())).is_none())
                    .map(|(id, members)| (id.clone(), members.clone()))
                    .collect();
                let (writes, _) = (self.fresh)(&legacy);
                for (id, members) in self.factories {
                    let key = Rejected::Factory(id.clone());
                    if context.read(key.clone()).is_none() && !(self.keep)(id, members, &writes) {
                        context.propose(key, ());
                    }
                }
                for id in self.legacy.keys() {
                    if !self.factories.contains_key(id) {
                        context.propose(Rejected::Factory(id.clone()), ());
                    }
                }
                for (candidates, record) in [(self.no_reentry, false), (self.records, true)] {
                    for (id, members) in candidates {
                        let key = if record {
                            Rejected::NoReentryRecord(id.clone())
                        } else {
                            Rejected::NoReentry(id.clone())
                        };
                        if context.read(key.clone()).is_none()
                            && (!self
                                .independent
                                .get(id)
                                .is_some_and(|independent| members.is_subset(independent))
                                || !(self.keep)(id, members, &writes))
                        {
                            context.propose(key, ());
                        }
                    }
                }
            }
            fn merge(_: &Rejected, _: &mut (), _: ()) -> bool {
                false
            }
        }
        let derive_fresh = |legacy: &Members| {
            if self.dynamic_scope_owners.is_empty() {
                fresh_value_writes(fresh_steps, namespace_value_facts, &factory_imports, legacy)
            } else {
                (BTreeSet::new(), BTreeSet::new())
            }
        };
        let keep_audited =
            |id: &Id, members: &BTreeSet<String>, writes: &BTreeSet<SourceUnitOrdinal>| {
                if namespace_family.is_empty() {
                    keep_namespace(id, members, writes)
                } else {
                    // Imported names do not establish distinct object identities.
                    namespace_family
                        .iter()
                        .all(|root| keep_namespace(root, &family_audited_members, writes))
                }
            };
        let completed = IterativeInference::new(NamespaceRules {
            factories: &self.audited_factory_members,
            legacy: &self.callback_factory_members,
            no_reentry: &self.no_reentry_members,
            records: &self.no_reentry_record_members,
            independent: &self.namespace_privacy.members,
            fresh: &derive_fresh,
            keep: &keep_audited,
        })
        .solve([()]);
        let retained_legacy: Members = self
            .callback_factory_members
            .iter()
            .filter(|(id, _)| completed.get(&Rejected::Factory((*id).clone())).is_none())
            .map(|(id, members)| (id.clone(), members.clone()))
            .collect();
        let (_, instance_field_writes) = derive_fresh(&retained_legacy);
        self.fresh_instance_field_writes = instance_field_writes;
        self.audited_factory_members
            .retain(|id, _| completed.get(&Rejected::Factory(id.clone())).is_none());
        self.callback_factory_members = retained_legacy;
        self.no_reentry_members
            .retain(|id, _| completed.get(&Rejected::NoReentry(id.clone())).is_none());
        self.no_reentry_record_members.retain(|id, _| {
            completed
                .get(&Rejected::NoReentryRecord(id.clone()))
                .is_none()
        });
        self.rejected_namespace_member_writes = namespace_member_write_witnesses.into_inner();
        self.rejected_namespace_dynamic_property_writes =
            namespace_dynamic_property_write_witnesses.into_inner();
        self.rejected_namespace_call_arguments = namespace_call_argument_witnesses.into_inner();
    }
}
