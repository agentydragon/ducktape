//! Native write census collection and per-point native effects.

use super::ExecutionIndex;
use super::deferred_global_publication::confined_prepublication_alias;
use super::global_receiver::{call_exposes_global_receiver, exposes_global_receiver};
use super::intrinsic_roots::{
    IntrinsicRoots, NativeReceiverRoot, close_raw_intrinsic_aliases, intrinsic_roots,
    native_mutation_roots, project_receiver_roots, receiver_global_property,
    receiver_uses_intrinsic_alias,
};
use super::native_write_census::{
    ConditionalIntrinsicWrite, NativePointEffects, NativeWriteCensus, native_write_slots,
    resolve_native_integrity, resolve_native_integrity_roots,
};
use super::property_keys::{PropertyKeyDomain, property_key_evidence};
use super::receiver_flow::{CallableReturnInputs, close_receiver_flow};
use analysis::facts::semantic_inference::{NativePremise, native_premise};
use analysis_domain::item_views::TopLevelItemView;
use analysis_vocabulary::inputs::{AnalysisInputs, HostBehaviorKey, NativeIntrinsic};
use analysis_vocabulary::report_schema::{
    NativeMutationOperation, NativeMutationWitness, NativePropertyKey, NativePrototypeMutationKind,
    NativeReceiverPath, NativeWriteSlot,
};
use source_identity::SourceUnitOrdinal;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;
use swc_common::{DUMMY_SP, Span, Spanned};
use swc_ecma_ast::*;
use value_flow::ChunkValueFlow;
use value_model::ValueExpr;

impl ExecutionIndex {
    /// Closes receiver flow, validates value inputs and resolves the native
    /// write census. Returns the mutated intrinsic roots.
    pub(super) fn census_native_writes(
        &mut self,
        body: &[TopLevelItemView<'_>],
        hints: &AnalysisInputs,
        flow: Option<&ChunkValueFlow>,
        namespace_assignments: &[&(Id, ValueExpr)],
    ) -> IntrinsicRoots {
        // Track aliases to native and explicitly captured ordinary values in
        // one receiver domain. The first pass is source-only and is used to
        // reject recognized contradictions before any author input is seeded.
        let declared = self.declared_bindings();
        for (callee, summary) in &hints.imported_callables {
            if summary.may_read_importer_cells.is_empty() {
                continue;
            }
            let Some(binding) = self
                .imported_bindings
                .iter()
                .find(|id| id.0.as_ref() == callee.as_str())
            else {
                continue;
            };
            let cells = self
                .exported_bindings
                .iter()
                .filter(|id| summary.may_read_importer_cells.contains(id.0.as_ref()))
                .cloned()
                .collect();
            self.importer_cell_reads.insert(binding.clone(), cells);
        }
        if self.census_globals.predefined != spec::PredefinedGlobals::Unknown {
            self.census_globals.bundle =
                self.bundle_globals(&hints.assumptions.bundle_globals, &declared);
        }
        let raw_intrinsic_aliases =
            close_raw_intrinsic_aliases(namespace_assignments, &self.census_globals, &declared);
        self.validate_value_inputs(
            &hints.selected_value_inputs,
            &hints.assumptions.captured_value_inputs,
            &raw_intrinsic_aliases,
            &declared,
        );
        self.validate_captured_callable_returns(
            namespace_assignments,
            flow,
            &raw_intrinsic_aliases,
        );
        let (callable_return_inputs, intrinsic_aliases) = if self.resolved_value_inputs.is_empty() {
            (
                CallableReturnInputs::default(),
                raw_intrinsic_aliases.clone(),
            )
        } else {
            let receiver_closure =
                close_receiver_flow(&*self, namespace_assignments, flow, &raw_intrinsic_aliases);
            (receiver_closure.callable_returns, receiver_closure.bindings)
        };
        let mut mutated_intrinsics = IntrinsicRoots::new();
        let mut unconditional_witnesses = Vec::new();
        let mut binding_write_sites = BTreeSet::new();
        for (owner, assignments) in self
            .top_assignments
            .iter()
            .enumerate()
            .map(|(owner, values)| (SourceUnitOrdinal(owner), values))
            .chain(
                self.callables
                    .values()
                    .map(|callable| (callable.owner, &callable.local_assignments)),
            )
        {
            for (id, _) in assignments {
                match id.0.as_ref() {
                    "TextEncoder" => {
                        mutated_intrinsics.insert(NativeIntrinsic::TextEncoder);
                    }
                    "Error" => {
                        mutated_intrinsics.insert(NativeIntrinsic::Error);
                    }
                    "Function" => {
                        mutated_intrinsics.insert(NativeIntrinsic::Function);
                    }
                    "Object" => {
                        mutated_intrinsics.insert(NativeIntrinsic::Object);
                    }
                    "Array" => {
                        mutated_intrinsics.insert(NativeIntrinsic::Array);
                    }
                    "Math" => {
                        mutated_intrinsics.insert(NativeIntrinsic::Math);
                    }
                    "RegExp" => {
                        mutated_intrinsics.insert(NativeIntrinsic::RegExp);
                    }
                    "Symbol" => {
                        mutated_intrinsics.insert(NativeIntrinsic::Symbol);
                    }
                    "Promise" => {
                        mutated_intrinsics.insert(NativeIntrinsic::Promise);
                    }
                    "Map" => {
                        mutated_intrinsics.insert(NativeIntrinsic::Map);
                    }
                    "Set" => {
                        mutated_intrinsics.insert(NativeIntrinsic::Set);
                    }
                    "WeakMap" => {
                        mutated_intrinsics.insert(NativeIntrinsic::WeakMap);
                    }
                    "WeakSet" => {
                        mutated_intrinsics.insert(NativeIntrinsic::WeakSet);
                    }
                    "FinalizationRegistry" => {
                        mutated_intrinsics.insert(NativeIntrinsic::FinalizationRegistry);
                    }
                    _ => {}
                }
                // Rebinding an unshadowed global-object alias
                // (`globalThis = other`) leaves every write through that name,
                // earlier or later, on an unknown receiver.
                let root = self
                    .census_globals
                    .binding_root(id.0.as_ref())
                    .filter(|root| {
                        *root != NativeIntrinsic::GlobalObject
                            || self.census_globals.contains(id.0.as_ref())
                    });
                if root == Some(NativeIntrinsic::GlobalObject) {
                    mutated_intrinsics.insert(NativeIntrinsic::GlobalObject);
                }
                if let Some(root) = root
                    && binding_write_sites.insert((owner, root))
                {
                    unconditional_witnesses.push(NativeMutationWitness {
                        chunk_id: String::new(),
                        statement_ordinal: owner,
                        source_location: None,
                        operation: NativeMutationOperation::BindingAssignment,
                        receiver_roots: BTreeSet::from([root]),
                        through_local_alias: false,
                        receiver_global: None,
                        affected_roots: BTreeSet::from([root]),
                        target_slots: if root == NativeIntrinsic::GlobalObject {
                            BTreeSet::from([NativeWriteSlot {
                                receiver: NativeReceiverPath::Unknown,
                                key: NativePropertyKey::Unknown,
                            }])
                        } else if self.property_key_declarations.contains(id) {
                            BTreeSet::new()
                        } else {
                            BTreeSet::from([NativeWriteSlot {
                                receiver: NativeReceiverPath::GlobalObject,
                                key: NativePropertyKey::String(id.0.to_string()),
                            }])
                        },
                        key_dependencies: BTreeSet::new(),
                        invalidated_key_dependencies: BTreeSet::new(),
                        value_input_refs: BTreeSet::new(),
                        value_input_evidence: Vec::new(),
                        unreachable_branch: None,
                        span: body[owner.0].as_module_item().span(),
                    });
                }
            }
        }
        if !self.dynamic_scope_owners.is_empty() {
            mutated_intrinsics.insert(NativeIntrinsic::GlobalObject);
        }
        self.native_write_census.unconditional = mutated_intrinsics;
        self.native_write_census.witnesses = Arc::new(unconditional_witnesses);
        for write in &self.intrinsic_writes {
            // Slot evidence is source-only: author-conditioned receiver
            // refinements may narrow family roots, but cannot certify that
            // their own native-operation premise was not contradicted.
            let source_roots = intrinsic_roots(
                &write.receiver,
                &raw_intrinsic_aliases,
                &self.census_globals,
                &declared,
                &BTreeMap::new(),
                &CallableReturnInputs::default(),
            );
            let key_evidence = write
                .key
                .as_ref()
                .map(|key| property_key_evidence(key, &self.property_key_assignments, &declared));
            let additional_key_evidence: Vec<_> = write
                .additional_keys
                .iter()
                .map(|key| property_key_evidence(key, &self.property_key_assignments, &declared))
                .collect();
            let mut target_slots = native_write_slots(
                &source_roots,
                &key_evidence
                    .as_ref()
                    .map_or(PropertyKeyDomain::Unknown, |evidence| {
                        evidence.domain.clone()
                    }),
            );
            for evidence in &additional_key_evidence {
                target_slots.extend(native_write_slots(&source_roots, &evidence.domain));
            }
            let receiver_roots = intrinsic_roots(
                &write.receiver,
                &intrinsic_aliases,
                &self.census_globals,
                &declared,
                &self.ordinary_input_refs,
                &callable_return_inputs,
            );
            let projection = project_receiver_roots(&receiver_roots, write.owner);
            if projection.bundle_global {
                self.native_write_census
                    .host_behavior
                    .insert(HostBehaviorKey::PredefinedGlobals);
            }
            let input_evidence = projection
                .value_input_refs
                .iter()
                .filter_map(|reference| self.value_input_evidence.get(reference).cloned())
                .collect();
            let roots = projection.effective_roots.clone();
            let witness = NativeMutationWitness {
                chunk_id: String::new(),
                statement_ordinal: write.owner,
                source_location: None,
                operation: write.operation,
                receiver_roots: projection.prior_roots,
                through_local_alias: receiver_uses_intrinsic_alias(
                    &write.receiver,
                    &intrinsic_aliases,
                ),
                receiver_global: receiver_global_property(
                    &write.receiver,
                    &intrinsic_aliases,
                    &self.census_globals,
                    &declared,
                ),
                affected_roots: projection.affected_roots,
                target_slots,
                key_dependencies: key_evidence
                    .as_ref()
                    .into_iter()
                    .chain(&additional_key_evidence)
                    .flat_map(|evidence| &evidence.dependencies)
                    .copied()
                    .chain(
                        source_roots
                            .contains(&NativeReceiverRoot::ArrayIteratorPrototype)
                            .then_some([
                                NativeIntrinsic::Object,
                                NativeIntrinsic::Array,
                                NativeIntrinsic::Symbol,
                            ])
                            .into_iter()
                            .flatten(),
                    )
                    .chain(
                        source_roots
                            .contains(&NativeReceiverRoot::SetIteratorPrototype)
                            .then_some([
                                NativeIntrinsic::Object,
                                NativeIntrinsic::Set,
                                NativeIntrinsic::Symbol,
                            ])
                            .into_iter()
                            .flatten(),
                    )
                    .collect(),
                invalidated_key_dependencies: BTreeSet::new(),
                value_input_refs: projection.value_input_refs,
                value_input_evidence: input_evidence,
                unreachable_branch: None,
                span: write.span,
            };
            let Some(_) = &write.key else {
                if !witness.affected_roots.is_empty() || !witness.value_input_refs.is_empty() {
                    Arc::make_mut(&mut self.native_write_census.witnesses).push(witness);
                }
                self.native_write_census
                    .unconditional
                    .extend(native_mutation_roots(roots));
                continue;
            };
            let evidence = key_evidence.expect("write with key has key evidence");
            let fallback = native_mutation_roots(roots.clone());
            let mut refined: IntrinsicRoots = roots
                .iter()
                .filter(|root| **root != NativeIntrinsic::GlobalObject)
                .copied()
                .collect();
            if roots.contains(&NativeIntrinsic::GlobalObject) {
                match &evidence.domain {
                    PropertyKeyDomain::FiniteStrings(keys) => {
                        for key in keys {
                            refined.extend(self.census_globals.binding_root(key));
                        }
                    }
                    PropertyKeyDomain::Affixed { prefix, suffix } => {
                        let key = NativePropertyKey::Affixed {
                            prefix: prefix.clone(),
                            suffix: suffix.clone(),
                        };
                        refined.extend(
                            self.census_globals
                                .binding_names()
                                .filter(|name| key.may_be_string(name))
                                .filter_map(|name| self.census_globals.binding_root(name)),
                        );
                    }
                    PropertyKeyDomain::Symbol | PropertyKeyDomain::SymbolIterator => {}
                    PropertyKeyDomain::Unknown => {
                        refined.extend(&fallback);
                    }
                }
            }
            self.native_write_census
                .conditional
                .push(ConditionalIntrinsicWrite {
                    witness,
                    refined: native_mutation_roots(refined),
                    fallback,
                    dependencies: evidence.dependencies,
                });
        }
        self.native_write_census.prototype_mutations = Arc::new(self.collect_prototype_mutations(
            &raw_intrinsic_aliases,
            namespace_assignments,
            &declared,
        ));
        let proofs = self.native_branch_proofs(&hints.imported_callables);
        for (write, proof) in self.native_write_census.conditional.iter_mut().zip(proofs) {
            write.witness.unreachable_branch = proof;
        }
        let mut censuses = vec![&self.native_write_census];
        let external = NativeWriteCensus {
            host_behavior: hints
                .native_integrity
                .as_ref()
                .map(|integrity| integrity.consumed_host_behavior.clone())
                .unwrap_or_default(),
            prototype_mutations: hints
                .native_integrity
                .as_ref()
                .map(|integrity| integrity.prototype_mutations.clone())
                .unwrap_or_default(),
            witnesses: hints
                .native_integrity
                .as_ref()
                .map(|integrity| integrity.witnesses.clone())
                .unwrap_or_default(),
            unconditional: hints
                .native_integrity
                .as_ref()
                .map(|integrity| integrity.mutated.clone())
                .unwrap_or_default(),
            conditional: Vec::new(),
        };
        censuses.push(&external);
        let mutated_intrinsics = if let Some(integrity) = &hints.native_integrity {
            self.native_integrity_witnesses = integrity.witnesses.clone();
            self.native_prototype_mutations = integrity.prototype_mutations.clone();
            self.never_written = integrity.never_written.clone();
            self.never_written_overrode = !integrity.never_written_overrides.is_empty();
            let mut mutated = resolve_native_integrity_roots(&censuses);
            mutated.retain(|root| !integrity.never_written.contains(root));
            mutated
        } else {
            let integrity = resolve_native_integrity(censuses);
            self.native_integrity_host_behavior = integrity.consumed_host_behavior;
            self.native_integrity_witnesses = integrity.witnesses;
            self.native_prototype_mutations = integrity.prototype_mutations;
            integrity.mutated
        };
        self.mutated_intrinsic_roots = mutated_intrinsics.clone();
        self.populate_native_point_effects(&external);
        mutated_intrinsics
    }

    pub(super) fn populate_native_point_effects(&mut self, external: &NativeWriteCensus) {
        // The structural visitor is the single source for activation. A
        // source-unit ordinal alone cannot distinguish a function declaration
        // from a synchronous invocation of its body.
        let mut activations =
            BTreeMap::<(SourceUnitOrdinal, u32, u32), BTreeSet<Option<u32>>>::new();
        let mut site = |owner: SourceUnitOrdinal, callable: Option<u32>, span: Span| {
            if span != DUMMY_SP {
                activations
                    .entry((owner, span.lo.0, span.hi.0))
                    .or_default()
                    .insert(callable);
            }
        };
        for write in &self.intrinsic_writes {
            site(write.owner, write.activation_callable, write.span);
        }
        for call in &self.prototype_calls {
            site(call.owner, call.caller, call.span);
        }
        for (owner, callable, span, _) in &self.prototype_field_writes {
            site(*owner, *callable, *span);
        }
        let activation = |owner: SourceUnitOrdinal, span: Span| {
            if span == DUMMY_SP {
                return None;
            }
            activations
                .get(&(owner, span.lo.0, span.hi.0))
                .and_then(|candidates| {
                    (candidates.len() == 1).then(|| *candidates.iter().next().unwrap())
                })
        };
        let mut initial = NativePointEffects::default();
        let is_foreign = |chunk_id: &str| self.source_chunk_id.as_deref() != Some(chunk_id);
        for mutation in external
            .prototype_mutations
            .iter()
            .filter(|mutation| is_foreign(&mutation.chunk_id))
        {
            initial.add_prototype_mutation(mutation);
        }
        for witness in external
            .witnesses
            .iter()
            .filter(|witness| is_foreign(&witness.chunk_id))
        {
            if witness.unreachable_branch.is_none() {
                initial.roots.extend(&witness.affected_roots);
            }
        }
        // The supplied census covers this chunk too. Only roots lacking a
        // source witness in that completed census are necessarily active at
        // entry. A foreign witness stays active even when a local write has
        // the same root.
        let witnessed_roots: IntrinsicRoots = external
            .witnesses
            .iter()
            .filter(|witness| witness.unreachable_branch.is_none())
            .flat_map(|witness| witness.affected_roots.iter().copied())
            .collect();
        initial
            .roots
            .extend(external.unconditional.difference(&witnessed_roots).copied());
        let local_witnessed_roots: IntrinsicRoots = self
            .native_write_census
            .witnesses
            .iter()
            .filter(|witness| witness.unreachable_branch.is_none())
            .flat_map(|witness| witness.affected_roots.iter().copied())
            .chain(
                self.native_write_census
                    .conditional
                    .iter()
                    .filter(|write| write.witness.unreachable_branch.is_none())
                    .flat_map(|write| write.fallback.iter().copied()),
            )
            .collect();
        initial.roots.extend(
            self.native_write_census
                .unconditional
                .difference(&local_witnessed_roots)
                .copied(),
        );
        // Dynamic scope is the only unconditional root without a source
        // witness. Keep it active even if another write has the same family.
        if self
            .native_write_census
            .prototype_mutations
            .iter()
            .chain(external.prototype_mutations.iter())
            .any(|mutation| mutation.kind == NativePrototypeMutationKind::DynamicScope)
        {
            initial.roots.insert(NativeIntrinsic::GlobalObject);
        }
        let mut by_activation = BTreeMap::<_, NativePointEffects>::new();
        for witness in self.native_write_census.witnesses.iter() {
            if witness.unreachable_branch.is_some() || witness.affected_roots.is_empty() {
                continue;
            }
            if let Some(callable) = activation(witness.statement_ordinal, witness.span) {
                by_activation
                    .entry((witness.statement_ordinal, callable))
                    .or_default()
                    .roots
                    .extend(&witness.affected_roots);
            } else {
                initial.roots.extend(&witness.affected_roots);
            }
        }
        for write in &self.native_write_census.conditional {
            if write.witness.unreachable_branch.is_some() {
                continue;
            }
            if let Some(callable) = activation(write.witness.statement_ordinal, write.witness.span)
            {
                by_activation
                    .entry((write.witness.statement_ordinal, callable))
                    .or_default()
                    .roots
                    .extend(&write.fallback);
            } else {
                initial.roots.extend(&write.fallback);
            }
        }
        for mutation in self.native_write_census.prototype_mutations.iter() {
            let mut mutation = mutation.clone();
            if !mutation
                .key_dependencies
                .is_disjoint(&self.mutated_intrinsic_roots)
                || (!mutation.key_dependencies.is_empty()
                    && self
                        .mutated_intrinsic_roots
                        .contains(&NativeIntrinsic::GlobalObject))
            {
                mutation.properties = None;
            }
            if let Some(callable) = activation(mutation.statement_ordinal, mutation.span) {
                by_activation
                    .entry((mutation.statement_ordinal, callable))
                    .or_default()
                    .add_prototype_mutation(&mutation);
            } else {
                initial.add_prototype_mutation(&mutation);
            }
        }
        let mut all = initial.clone();
        for effects in by_activation.values() {
            all.extend(effects);
        }
        self.native_point_effects = by_activation;
        self.initial_native_point_effects = initial;
        self.all_native_point_effects = all;
    }

    /// Native slot proof for one indexed own-data read.
    pub(in super::super) fn native_own_data_read_premise(
        &self,
        span: Span,
    ) -> Option<NativePremise> {
        if !self.dynamic_scope_owners.is_empty() {
            return None;
        }
        let slots = self.native_own_data_reads.get(&span.lo.0)?;
        native_premise(
            &self.integrity,
            slots,
            Some(span.lo.0),
            analysis_vocabulary::provenance::InferenceRule::NativeOwnDataRead,
        )
    }

    pub(super) fn global_receiver_exposed(
        &self,
        first_publication: SourceUnitOrdinal,
        protected: &BTreeSet<String>,
        certified_root_ids: &BTreeSet<Id>,
    ) -> bool {
        self.top_assignments
            .iter()
            .flatten()
            .any(|(_, value)| exposes_global_receiver(value, &self.global_object_names))
            || self
                .top_exported_values
                .iter()
                .flatten()
                .any(|value| exposes_global_receiver(value, &self.global_object_names))
            || self.top_calls.iter().flatten().any(|call| {
                call_exposes_global_receiver(call, &self.global_object_names, certified_root_ids)
            })
            || self.callables.iter().any(|(key, callable)| {
                callable.local_assignments.iter().any(|(id, value)| {
                    exposes_global_receiver(value, &self.global_object_names)
                        && !confined_prepublication_alias(
                            self,
                            *key,
                            id,
                            first_publication,
                            protected,
                        )
                }) || callable
                    .body_facts
                    .value_flow()
                    .returns()
                    .iter()
                    .any(|value| exposes_global_receiver(value, &self.global_object_names))
                    || callable.calls.iter().any(|call| {
                        call_exposes_global_receiver(
                            call,
                            &self.global_object_names,
                            certified_root_ids,
                        )
                    })
            })
    }
}
