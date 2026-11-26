//! Native write slot classification and integrity closure.
//!
//! Array-spread integrity uses a premise-relative closure that stays separate
//! from ordinary native-integrity inference.

use super::property_keys::{PropertyKeyDomain, PropertyKeyExpr};
use super::{IntrinsicRoots, NativeReceiverRoot, NativeReceiverRoots, ValueExpr};
use analysis::analysis_inputs::{HostBehaviorKey, NativeIntegrity, NativeIntrinsic};
use analysis::inference::{FactContext, IterativeInference, IterativeRules};
use analysis::reports::schema::{
    NativeMutationOperation, NativeMutationWitness, NativePropertyKey, NativePrototypeMutation,
    NativeReceiverPath, NativeWriteSlot,
};
use binding_targets;
use source_identity::SourceUnitOrdinal;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;
use swc_common::Span;
use swc_ecma_ast::{Expr, MemberExpr, MemberProp};

pub(super) fn native_write_slots(
    roots: &NativeReceiverRoots,
    key: &PropertyKeyDomain,
) -> BTreeSet<NativeWriteSlot> {
    let keys: Vec<_> = match key {
        PropertyKeyDomain::FiniteStrings(values) => values
            .iter()
            .cloned()
            .map(NativePropertyKey::String)
            .collect(),
        PropertyKeyDomain::Affixed { prefix, suffix } => vec![NativePropertyKey::Affixed {
            prefix: prefix.clone(),
            suffix: suffix.clone(),
        }],
        PropertyKeyDomain::SymbolIterator => vec![NativePropertyKey::SymbolIterator],
        PropertyKeyDomain::Symbol => vec![NativePropertyKey::OtherSymbol],
        PropertyKeyDomain::Unknown => vec![NativePropertyKey::Unknown],
    };
    let roots = if roots.is_empty() {
        BTreeSet::from([NativeReceiverRoot::Intrinsic(
            NativeIntrinsic::UnknownGlobalValue,
        )])
    } else {
        roots.clone()
    };
    roots
        .iter()
        .flat_map(|root| {
            let receiver = match root {
                NativeReceiverRoot::Intrinsic(NativeIntrinsic::GlobalObject) => {
                    NativeReceiverPath::GlobalObject
                }
                NativeReceiverRoot::Intrinsic(NativeIntrinsic::UnknownGlobalValue)
                | NativeReceiverRoot::OrdinaryDirect { .. }
                | NativeReceiverRoot::OrdinaryDescendant { .. }
                | NativeReceiverRoot::BundleGlobal => NativeReceiverPath::Unknown,
                NativeReceiverRoot::Intrinsic(root) => NativeReceiverPath::Constructor(*root),
                NativeReceiverRoot::IntrinsicPrototype(root) => {
                    NativeReceiverPath::Prototype(*root)
                }
                NativeReceiverRoot::IntrinsicDescendant(root) => {
                    NativeReceiverPath::Descendant(*root)
                }
                NativeReceiverRoot::ArrayIteratorPrototype => {
                    NativeReceiverPath::ArrayIteratorPrototype
                }
                NativeReceiverRoot::SetIteratorPrototype => {
                    NativeReceiverPath::SetIteratorPrototype
                }
            };
            keys.iter().cloned().map(move |key| NativeWriteSlot {
                receiver: receiver.clone(),
                key,
            })
        })
        .collect()
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub(super) enum NativeSlotOverlap {
    Known(SourceUnitOrdinal),
    Opaque,
    Absent,
}

/// The Array-spread operation input supplies this one narrow native-iterator
/// premise: an iterator from a fresh array uses the native iterator protocol.
/// It can identify a write to that iterator's `next` slot, but does not prove
/// Object or Symbol identity.
#[derive(Clone, Copy)]
pub(super) struct ArraySpreadNativeIteratorPremise;

pub(super) fn array_spread_protocol_slot_overlap<'a>(
    witnesses: impl Iterator<Item = &'a NativeMutationWitness>,
    slot: &NativeWriteSlot,
    source_mutated: &IntrinsicRoots,
    global_array_binding_mutated: bool,
    premise_authorized_next_writes: &BTreeSet<ArrayIteratorNextWriteId>,
    _premise: ArraySpreadNativeIteratorPremise,
) -> NativeSlotOverlap {
    let (expected_dependencies, independently_required, premise_allowed) =
        match (&slot.receiver, &slot.key) {
            (
                NativeReceiverPath::Prototype(NativeIntrinsic::Array),
                NativePropertyKey::SymbolIterator,
            ) => (
                IntrinsicRoots::from([NativeIntrinsic::Symbol]),
                IntrinsicRoots::from([NativeIntrinsic::Symbol]),
                IntrinsicRoots::new(),
            ),
            (NativeReceiverPath::ArrayIteratorPrototype, NativePropertyKey::String(property))
                if property == "next" =>
            {
                (
                    IntrinsicRoots::from([
                        NativeIntrinsic::Object,
                        NativeIntrinsic::Array,
                        NativeIntrinsic::Symbol,
                    ]),
                    IntrinsicRoots::from([NativeIntrinsic::Object, NativeIntrinsic::Symbol]),
                    // The selected Array-spread premise supplies only the Array
                    // iterator receiver identity; Object.getPrototypeOf and the
                    // Symbol.iterator key still require independent source proof.
                    IntrinsicRoots::from([NativeIntrinsic::Array]),
                )
            }
            _ => return NativeSlotOverlap::Absent,
        };
    let mut opaque = false;
    for witness in witnesses {
        if witness.unreachable_branch.is_some() || !witness.target_slots.contains(slot) {
            continue;
        }
        let is_array_iterator_next = matches!(
            (&slot.receiver, &slot.key),
            (
                NativeReceiverPath::ArrayIteratorPrototype,
                NativePropertyKey::String(property)
            ) if property == "next"
        );
        if is_array_iterator_next
            && !premise_authorized_next_writes.contains(&array_iterator_next_write_id(witness))
        {
            // The premise discharges only the receiver part of this merged
            // dependency set. A conditional key dependency on Array remains
            // unresolved unless this exact write was independently eligible
            // for the premise-relative closure.
            opaque = true;
            continue;
        }
        // The expected sets distinguish the canonical receiver dependency
        // from any uncertainty in a computed property key or alternative
        // receiver path. Do not discharge arbitrary roots from the premise.
        if witness.target_slots.len() != 1
            || witness.key_dependencies != expected_dependencies
            || !witness.value_input_refs.is_empty()
            || (matches!(
                (&slot.receiver, &slot.key),
                (
                    NativeReceiverPath::Prototype(NativeIntrinsic::Array),
                    NativePropertyKey::SymbolIterator
                )
            ) && global_array_binding_mutated)
            || source_mutated.contains(&NativeIntrinsic::GlobalObject)
            || !independently_required.is_disjoint(source_mutated)
            || !witness
                .key_dependencies
                .intersection(source_mutated)
                .all(|dependency| premise_allowed.contains(dependency))
        {
            opaque = true;
            continue;
        }
        return NativeSlotOverlap::Known(witness.statement_ordinal);
    }
    if opaque {
        NativeSlotOverlap::Opaque
    } else {
        NativeSlotOverlap::Absent
    }
}

pub(super) type ArrayIteratorNextWriteId = (String, SourceUnitOrdinal, u32, u32);

fn array_iterator_next_write_id(witness: &NativeMutationWitness) -> ArrayIteratorNextWriteId {
    (
        witness.chunk_id.clone(),
        witness.statement_ordinal,
        witness.span.lo.0,
        witness.span.hi.0,
    )
}

fn exact_array_iterator_next_candidate(witness: &NativeMutationWitness) -> bool {
    witness.unreachable_branch.is_none()
        && witness.value_input_refs.is_empty()
        && witness.target_slots.len() == 1
        && witness.target_slots.contains(&NativeWriteSlot {
            receiver: NativeReceiverPath::ArrayIteratorPrototype,
            key: NativePropertyKey::String("next".to_owned()),
        })
        && witness.key_dependencies
            == IntrinsicRoots::from([
                NativeIntrinsic::Object,
                NativeIntrinsic::Array,
                NativeIntrinsic::Symbol,
            ])
}

pub(super) fn source_slot_overlap<'a>(
    witnesses: impl Iterator<Item = &'a NativeMutationWitness>,
    slot: &NativeWriteSlot,
) -> NativeSlotOverlap {
    let mut opaque = false;
    for witness in witnesses {
        // A direct constructor/prototype path is exact relative to the
        // caller's native-identity premise. Descendant and cross-path aliases
        // may reach that same object, but remain opaque here.
        if matches!(
            &slot.receiver,
            NativeReceiverPath::GlobalObject
                | NativeReceiverPath::Constructor(_)
                | NativeReceiverPath::Prototype(_)
        ) && witness.target_slots.contains(slot)
            && matches!(
                &slot.key,
                NativePropertyKey::String(_) | NativePropertyKey::SymbolIterator
            )
            && witness.key_dependencies.is_empty()
        {
            return NativeSlotOverlap::Known(witness.statement_ordinal);
        }
        opaque |= witness.target_slots.iter().any(|candidate| {
            !witness.key_dependencies.is_empty()
                || candidate.key == slot.key
                || match &slot.key {
                    NativePropertyKey::String(name) => candidate.key.may_be_string(name),
                    _ => candidate.key == NativePropertyKey::Unknown,
                }
        });
    }
    if opaque {
        NativeSlotOverlap::Opaque
    } else {
        NativeSlotOverlap::Absent
    }
}

#[derive(Debug)]
pub(crate) struct IntrinsicWriteTarget {
    pub(super) owner: SourceUnitOrdinal,
    pub(super) activation_callable: Option<u32>,
    /// The constructor whose `this` a direct member operation writes.
    /// Destructuring and reflective calls stay opaque.
    pub(super) constructor_this: Option<u32>,
    pub(super) span: Span,
    pub(super) operation: NativeMutationOperation,
    pub(crate) receiver: ValueExpr,
    pub(super) key: Option<PropertyKeyExpr>,
    pub(super) additional_keys: Vec<PropertyKeyExpr>,
    /// The value an assignment stores; `None` for every other operation.
    pub(super) value: Option<ValueExpr>,
}

/// Conservative effects activated before a top-level item or callable body.
/// This deliberately has no within-body ordering: all of a body's writes
/// precede its first modeled native guard.
#[derive(Clone, Default)]
pub(crate) struct NativePointEffects {
    pub(crate) roots: IntrinsicRoots,
    prototype_site_count: usize,
    prototype_any_key: usize,
    prototype_known_keys: BTreeMap<String, usize>,
}

impl NativePointEffects {
    pub(crate) fn extend(&mut self, other: &Self) {
        self.roots.extend(&other.roots);
        self.prototype_site_count += other.prototype_site_count;
        self.prototype_any_key += other.prototype_any_key;
        for (property, count) in &other.prototype_known_keys {
            *self
                .prototype_known_keys
                .entry(property.clone())
                .or_default() += count;
        }
    }

    pub(super) fn add_prototype_mutation(&mut self, mutation: &NativePrototypeMutation) {
        self.prototype_site_count += 1;
        if let Some(properties) = &mutation.properties {
            for property in properties {
                *self
                    .prototype_known_keys
                    .entry(property.clone())
                    .or_default() += 1;
            }
        } else {
            self.prototype_any_key += 1;
        }
    }

    pub(crate) fn blocking_prototype_mutations(&self, property: Option<&str>) -> usize {
        match property {
            Some(property) => {
                self.prototype_any_key
                    + self
                        .prototype_known_keys
                        .get(property)
                        .copied()
                        .unwrap_or(0)
            }
            None => self.prototype_site_count,
        }
    }
}

pub(super) fn intrinsic_write_target(
    member: &MemberExpr,
    owner: SourceUnitOrdinal,
    receiver_callable: Option<u32>,
    activation_callable: Option<u32>,
    operation: NativeMutationOperation,
) -> IntrinsicWriteTarget {
    let key = match &member.prop {
        MemberProp::Ident(property) => PropertyKeyExpr::String(property.sym.to_string()),
        MemberProp::Computed(property) => PropertyKeyExpr::from_expr(&property.expr),
        _ => PropertyKeyExpr::Unknown,
    };
    IntrinsicWriteTarget {
        owner,
        activation_callable,
        constructor_this: receiver_callable.filter(|_| {
            matches!(
                binding_targets::transparent_target_expr(&member.obj),
                Expr::This(_)
            )
        }),
        span: member.span,
        operation,
        receiver: ValueExpr::from_expr(binding_targets::transparent_target_expr(&member.obj)),
        key: Some(key),
        additional_keys: Vec::new(),
        value: None,
    }
}

#[derive(Clone, Default)]
pub struct NativeWriteCensus {
    pub(super) unconditional: IntrinsicRoots,
    /// Host claims a receiver discharge consumed.
    pub(super) host_behavior: BTreeSet<HostBehaviorKey>,
    pub(super) prototype_mutations: Arc<Vec<NativePrototypeMutation>>,
    pub(super) witnesses: Arc<Vec<NativeMutationWitness>>,
    pub(super) conditional: Vec<ConditionalIntrinsicWrite>,
}

/// A premise-relative view used only to decide whether a selected Array-spread
/// input is contradicted by a source-known iterator protocol write. Its root
/// closure never feeds ordinary native-integrity inference.
pub struct ArraySpreadProtocolIntegrity {
    pub(super) mutated: IntrinsicRoots,
    pub(super) global_array_binding_mutated: bool,
    pub(super) premise_authorized_next_writes: BTreeSet<ArrayIteratorNextWriteId>,
}

impl NativeWriteCensus {
    fn for_array_spread_protocol_query(&self) -> (Self, BTreeSet<ArrayIteratorNextWriteId>) {
        let mut census = self.clone();
        let mut premise_authorized_next_writes = BTreeSet::new();
        for write in &mut census.conditional {
            if !write.dependencies.is_empty()
                || !exact_array_iterator_next_candidate(&write.witness)
            {
                continue;
            }
            // The typed Array-spread premise identifies only the iterator
            // receiver. Keep the original fallback for a failed independent
            // Object/Symbol proof, and retain the Array effect so other
            // conditional writes still see this mutation in the fixed point.
            write.refined = IntrinsicRoots::from([NativeIntrinsic::Array]);
            write.dependencies =
                IntrinsicRoots::from([NativeIntrinsic::Object, NativeIntrinsic::Symbol]);
            premise_authorized_next_writes.insert(array_iterator_next_write_id(&write.witness));
        }
        (census, premise_authorized_next_writes)
    }
}

#[derive(Clone)]
pub(super) struct ConditionalIntrinsicWrite {
    pub(super) witness: NativeMutationWitness,
    pub(super) refined: IntrinsicRoots,
    pub(super) fallback: IntrinsicRoots,
    pub(super) dependencies: IntrinsicRoots,
}

struct NativeIntegrityRootRules<'a> {
    writes: Vec<&'a ConditionalIntrinsicWrite>,
}

impl IterativeRules for NativeIntegrityRootRules<'_> {
    type Key = ();
    type Fact = IntrinsicRoots;
    type Rule = usize;

    type MergeScratch = ();
    fn evaluate(&self, rule: &Self::Rule, context: &mut impl FactContext<Self::Key, Self::Fact>) {
        let write = self.writes[*rule];
        let invalid = {
            let Some(mutated) = context.read(()) else {
                return;
            };
            mutated.contains(&NativeIntrinsic::GlobalObject)
                || !write.dependencies.is_disjoint(mutated)
        };
        context.propose(
            (),
            if invalid {
                write.fallback.clone()
            } else {
                write.refined.clone()
            },
        );
    }

    fn merge(_: &Self::Key, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
        let previous_len = current.len();
        current.extend(contribution);
        current.len() != previous_len
    }
}

/// A11 supplies the initial native identities. Every retained write either
/// preserves those identities under its operation premises or invalidates the
/// affected families. Propagate invalidation to a fixed point, including writes
/// in deferred bodies. This is a closed-write invariant, not self-certification
/// from the absence of a fact produced by the same candidate.
pub fn resolve_native_integrity<'a>(
    censuses: impl IntoIterator<Item = &'a NativeWriteCensus>,
) -> NativeIntegrity {
    let censuses: Vec<_> = censuses.into_iter().collect();
    let mutated = resolve_native_integrity_roots(&censuses);
    let mut witnesses: Vec<_> = censuses
        .iter()
        .flat_map(|census| census.witnesses.iter().cloned())
        .collect();
    for write in censuses.iter().flat_map(|census| &census.conditional) {
        let mut witness = write.witness.clone();
        if witness.unreachable_branch.is_some() {
            witness.affected_roots.clear();
            witness.value_input_refs.clear();
            witness.value_input_evidence.clear();
            witnesses.push(witness);
            continue;
        }
        let invalid = mutated.contains(&NativeIntrinsic::GlobalObject)
            || !write.dependencies.is_disjoint(&mutated);
        witness.affected_roots = if invalid {
            write.fallback.clone()
        } else {
            write.refined.clone()
        };
        if invalid && write.refined != write.fallback {
            witness.invalidated_key_dependencies =
                write.dependencies.intersection(&mutated).copied().collect();
            if mutated.contains(&NativeIntrinsic::GlobalObject) {
                witness
                    .invalidated_key_dependencies
                    .insert(NativeIntrinsic::GlobalObject);
            }
        }
        if !witness.affected_roots.is_empty() || !witness.value_input_refs.is_empty() {
            witnesses.push(witness);
        }
    }
    let mut consumed_value_inputs = BTreeSet::new();
    let mut value_input_evidence = BTreeMap::new();
    for witness in &witnesses {
        if witness.unreachable_branch.is_some() {
            continue;
        }
        consumed_value_inputs.extend(witness.value_input_refs.iter().cloned());
        value_input_evidence.extend(
            witness
                .value_input_evidence
                .iter()
                .cloned()
                .map(|evidence| (evidence.reference.clone(), evidence)),
        );
    }
    NativeIntegrity {
        prototype_mutations: Arc::new(
            censuses
                .iter()
                .flat_map(|census| census.prototype_mutations.iter().cloned())
                .map(|mut mutation| {
                    if !mutation.key_dependencies.is_disjoint(&mutated)
                        || (!mutation.key_dependencies.is_empty()
                            && mutated.contains(&NativeIntrinsic::GlobalObject))
                    {
                        mutation.properties = None;
                        if mutated.contains(&NativeIntrinsic::GlobalObject) {
                            mutation
                                .key_dependencies
                                .insert(NativeIntrinsic::GlobalObject);
                        }
                    }
                    mutation
                })
                .collect(),
        ),
        mutated,
        witnesses: Arc::new(witnesses),
        value_input_evidence,
        consumed_value_inputs,
        consumed_host_behavior: censuses
            .iter()
            .flat_map(|census| census.host_behavior.iter().copied())
            .collect(),
        never_written: BTreeSet::new(),
        never_written_overrides: Vec::new(),
    }
}

/// Applies `program_premises.intrinsics_never_written`: the named families'
/// slots hold whatever the census witnesses, so they leave `mutated` and each
/// witness reaching them joins the audit list. A witness writing such a family
/// on its exact object refutes the premise; every such site is returned.
pub fn apply_intrinsics_never_written(
    integrity: &mut NativeIntegrity,
    families: &BTreeSet<NativeIntrinsic>,
) -> std::result::Result<(), Vec<String>> {
    use analysis::reports::schema::{NeverWrittenOverride, NeverWrittenReach};
    let exact_family = |slot: &NativeWriteSlot| match &slot.receiver {
        NativeReceiverPath::Constructor(family) | NativeReceiverPath::Prototype(family) => {
            families.contains(family).then_some(*family)
        }
        NativeReceiverPath::ArrayIteratorPrototype => families
            .contains(&NativeIntrinsic::Array)
            .then_some(NativeIntrinsic::Array),
        NativeReceiverPath::SetIteratorPrototype => families
            .contains(&NativeIntrinsic::Set)
            .then_some(NativeIntrinsic::Set),
        NativeReceiverPath::GlobalObject => families.iter().copied().find(|family| {
            matches!(&slot.key, NativePropertyKey::String(key)
                if family.global_binding() == Some(key.as_str()))
        }),
        NativeReceiverPath::Descendant(_) | NativeReceiverPath::Unknown => None,
    };
    let mut refutations = Vec::new();
    let mut overrides = Vec::new();
    for (index, witness) in integrity.witnesses.iter().enumerate() {
        if witness.unreachable_branch.is_some() {
            continue;
        }
        let location = || {
            format!(
                "{}{}",
                witness.chunk_id,
                witness
                    .source_location
                    .as_ref()
                    .map_or(String::new(), |location| format!(
                        ":{}:{}",
                        location.start_line,
                        location.start_column.unwrap_or_default()
                    ))
            )
        };
        let exact: BTreeSet<_> = if witness.operation == NativeMutationOperation::BindingAssignment
        {
            witness
                .affected_roots
                .intersection(families)
                .copied()
                .collect()
        } else {
            witness
                .target_slots
                .iter()
                .filter_map(exact_family)
                .collect()
        };
        if !exact.is_empty() {
            refutations.push(format!(
                "{} writes {exact:?} on its exact object",
                location()
            ));
            continue;
        }
        if witness.affected_roots.is_empty() {
            continue;
        }
        let unknown_receiver = witness
            .target_slots
            .iter()
            .any(|slot| slot.receiver == NativeReceiverPath::Unknown)
            || witness.target_slots.is_empty();
        let global_object = witness
            .affected_roots
            .contains(&NativeIntrinsic::GlobalObject);
        let (reach, reached) = if unknown_receiver {
            (NeverWrittenReach::UnknownReceiver, families.clone())
        } else if global_object {
            (NeverWrittenReach::GlobalObject, families.clone())
        } else {
            (
                NeverWrittenReach::Family,
                witness
                    .affected_roots
                    .intersection(families)
                    .copied()
                    .collect(),
            )
        };
        if reached.is_empty() {
            continue;
        }
        overrides.push(NeverWrittenOverride {
            witness: index,
            chunk_id: witness.chunk_id.clone(),
            source_location: witness.source_location.clone(),
            families: reached,
            reach,
        });
    }
    if !refutations.is_empty() {
        return Err(refutations);
    }
    integrity.mutated.retain(|root| !families.contains(root));
    integrity.never_written = families.clone();
    integrity.never_written_overrides = overrides;
    Ok(())
}

pub(super) fn resolve_native_integrity_roots(censuses: &[&NativeWriteCensus]) -> IntrinsicRoots {
    let mutated: IntrinsicRoots = censuses
        .iter()
        .flat_map(|census| census.unconditional.iter().copied())
        .collect();
    let rules = NativeIntegrityRootRules {
        writes: censuses
            .iter()
            .flat_map(|census| &census.conditional)
            .filter(|write| write.witness.unreachable_branch.is_none())
            .collect(),
    };
    let producer_count = rules.writes.len();
    let mut inference = IterativeInference::new(rules);
    inference.seed((), mutated);
    inference
        .solve(0..producer_count)
        .into_values()
        .remove(&())
        .expect("unconditional native roots seed the shared closure fact")
}

fn source_global_array_binding_write(censuses: &[&NativeWriteCensus]) -> bool {
    let slot = NativeWriteSlot {
        receiver: NativeReceiverPath::GlobalObject,
        key: NativePropertyKey::String("Array".to_owned()),
    };
    censuses.iter().any(|census| {
        census
            .witnesses
            .iter()
            .chain(census.conditional.iter().map(|write| &write.witness))
            .any(|witness| {
                witness.unreachable_branch.is_none() && witness.target_slots.contains(&slot)
            })
    })
}

pub fn resolve_array_spread_protocol_integrity<'a>(
    censuses: impl IntoIterator<Item = &'a NativeWriteCensus>,
) -> ArraySpreadProtocolIntegrity {
    let source_censuses: Vec<_> = censuses.into_iter().collect();
    let global_array_binding_mutated = source_global_array_binding_write(&source_censuses);
    let mut premise_authorized_next_writes = BTreeSet::new();
    let query_censuses: Vec<_> = source_censuses
        .iter()
        .map(|census| {
            let (query, authorized) = census.for_array_spread_protocol_query();
            premise_authorized_next_writes.extend(authorized);
            query
        })
        .collect();
    let query_refs: Vec<_> = query_censuses.iter().collect();
    ArraySpreadProtocolIntegrity {
        mutated: resolve_native_integrity_roots(&query_refs),
        global_array_binding_mutated,
        premise_authorized_next_writes,
    }
}

#[cfg(test)]
mod native_integrity_closure_tests {
    use super::{
        ArraySpreadNativeIteratorPremise, ConditionalIntrinsicWrite, IntrinsicRoots,
        NativeSlotOverlap, NativeWriteCensus, array_spread_protocol_slot_overlap,
        resolve_array_spread_protocol_integrity, resolve_native_integrity, source_slot_overlap,
    };
    use analysis::analysis_inputs::{
        CallbackFactoryEvidence, CallbackObjectArguments, NativeIntrinsic,
    };
    use analysis::reports::schema::{
        NativeMutationOperation, NativeMutationWitness, NativePropertyKey, NativeReceiverPath,
        NativeUnreachableBranch, NativeWriteSlot,
    };
    use source_identity::SourceUnitOrdinal;
    use std::collections::BTreeSet;
    use swc_common::DUMMY_SP;

    fn conditional_write(
        ordinal: usize,
        dependencies: IntrinsicRoots,
        refined: IntrinsicRoots,
        fallback: IntrinsicRoots,
        unreachable: bool,
    ) -> ConditionalIntrinsicWrite {
        ConditionalIntrinsicWrite {
            witness: NativeMutationWitness {
                chunk_id: "test".to_owned(),
                statement_ordinal: SourceUnitOrdinal(ordinal),
                source_location: None,
                operation: NativeMutationOperation::PropertyAssignment,
                receiver_roots: BTreeSet::new(),
                through_local_alias: false,
                receiver_global: None,
                affected_roots: BTreeSet::new(),
                target_slots: BTreeSet::new(),
                key_dependencies: BTreeSet::new(),
                invalidated_key_dependencies: BTreeSet::new(),
                value_input_refs: BTreeSet::new(),
                value_input_evidence: Vec::new(),
                unreachable_branch: unreachable.then(|| NativeUnreachableBranch {
                    arguments: CallbackObjectArguments {
                        wrapper: CallbackFactoryEvidence::AuthorAssertion {
                            defining_module: "test".to_owned(),
                            export: "wrapper".to_owned(),
                        },
                        object_arguments: BTreeSet::new(),
                    },
                    imported_local: "wrapper".to_owned(),
                    wrapper_call_location: None,
                    callback_location: None,
                    guard_location: None,
                    guard_span: DUMMY_SP,
                    wrapper_call_span: DUMMY_SP,
                    callback_span: DUMMY_SP,
                }),
                span: DUMMY_SP,
            },
            refined,
            fallback,
            dependencies,
        }
    }

    fn array_iterator_next_candidate(ordinal: usize) -> ConditionalIntrinsicWrite {
        let mut candidate = conditional_write(
            ordinal,
            IntrinsicRoots::new(),
            IntrinsicRoots::new(),
            IntrinsicRoots::from([NativeIntrinsic::GlobalObject]),
            false,
        );
        candidate.witness.target_slots.insert(NativeWriteSlot {
            receiver: NativeReceiverPath::ArrayIteratorPrototype,
            key: NativePropertyKey::String("next".to_owned()),
        });
        candidate.witness.key_dependencies = IntrinsicRoots::from([
            NativeIntrinsic::Object,
            NativeIntrinsic::Array,
            NativeIntrinsic::Symbol,
        ]);
        candidate
    }

    #[test]
    fn slot_query_distinguishes_known_global_write_from_alias_or_key_uncertainty() {
        let target = NativeWriteSlot {
            receiver: NativeReceiverPath::GlobalObject,
            key: NativePropertyKey::String("Symbol".to_owned()),
        };
        let mut witness = conditional_write(
            1,
            IntrinsicRoots::new(),
            IntrinsicRoots::new(),
            IntrinsicRoots::new(),
            false,
        )
        .witness;
        witness.target_slots.insert(target.clone());
        assert_eq!(
            source_slot_overlap(std::iter::once(&witness), &target),
            NativeSlotOverlap::Known(SourceUnitOrdinal(1))
        );

        witness.key_dependencies.insert(NativeIntrinsic::Symbol);
        assert_eq!(
            source_slot_overlap(std::iter::once(&witness), &target),
            NativeSlotOverlap::Opaque
        );

        witness.key_dependencies.clear();
        witness.target_slots.clear();
        witness.target_slots.insert(NativeWriteSlot {
            receiver: NativeReceiverPath::Descendant(NativeIntrinsic::Set),
            key: NativePropertyKey::String("Symbol".to_owned()),
        });
        assert_eq!(
            source_slot_overlap(std::iter::once(&witness), &target),
            NativeSlotOverlap::Opaque
        );

        let prototype_slot = NativeWriteSlot {
            receiver: NativeReceiverPath::Prototype(NativeIntrinsic::Set),
            key: NativePropertyKey::String("add".to_owned()),
        };
        witness.target_slots.clear();
        witness.target_slots.insert(prototype_slot.clone());
        assert_eq!(
            source_slot_overlap(std::iter::once(&witness), &prototype_slot),
            NativeSlotOverlap::Known(SourceUnitOrdinal(1))
        );
        witness.target_slots.clear();
        witness.target_slots.insert(NativeWriteSlot {
            receiver: NativeReceiverPath::Descendant(NativeIntrinsic::Set),
            key: NativePropertyKey::String("add".to_owned()),
        });
        assert_eq!(
            source_slot_overlap(std::iter::once(&witness), &prototype_slot),
            NativeSlotOverlap::Opaque
        );
        let wildcard = NativeWriteSlot {
            receiver: NativeReceiverPath::GlobalObject,
            key: NativePropertyKey::Unknown,
        };
        witness.target_slots.clear();
        witness.target_slots.insert(wildcard.clone());
        assert_eq!(
            source_slot_overlap(std::iter::once(&witness), &wildcard),
            NativeSlotOverlap::Opaque
        );
    }

    #[test]
    fn array_spread_slot_query_uses_only_independent_symbol_and_object_guards() {
        let premise = ArraySpreadNativeIteratorPremise;
        let query = |witness: &NativeMutationWitness,
                     slot: &NativeWriteSlot,
                     source_mutated: &IntrinsicRoots,
                     global_array_binding_mutated: bool,
                     authorize_next: bool| {
            let authorized = if authorize_next {
                BTreeSet::from([super::array_iterator_next_write_id(witness)])
            } else {
                BTreeSet::new()
            };
            array_spread_protocol_slot_overlap(
                std::iter::once(witness),
                slot,
                source_mutated,
                global_array_binding_mutated,
                &authorized,
                premise,
            )
        };
        let array_iterator_slot = NativeWriteSlot {
            receiver: NativeReceiverPath::Prototype(NativeIntrinsic::Array),
            key: NativePropertyKey::SymbolIterator,
        };
        let mut array_iterator_write = conditional_write(
            1,
            IntrinsicRoots::new(),
            IntrinsicRoots::new(),
            IntrinsicRoots::new(),
            false,
        )
        .witness;
        array_iterator_write
            .target_slots
            .insert(array_iterator_slot.clone());
        array_iterator_write
            .key_dependencies
            .insert(NativeIntrinsic::Symbol);
        assert_eq!(
            query(
                &array_iterator_write,
                &array_iterator_slot,
                &IntrinsicRoots::new(),
                false,
                false,
            ),
            NativeSlotOverlap::Known(SourceUnitOrdinal(1))
        );
        for mutated in [
            IntrinsicRoots::from([NativeIntrinsic::Symbol]),
            IntrinsicRoots::from([NativeIntrinsic::GlobalObject]),
        ] {
            assert_eq!(
                query(
                    &array_iterator_write,
                    &array_iterator_slot,
                    &mutated,
                    false,
                    false,
                ),
                NativeSlotOverlap::Opaque
            );
        }
        assert_eq!(
            query(
                &array_iterator_write,
                &array_iterator_slot,
                &IntrinsicRoots::new(),
                true,
                false,
            ),
            NativeSlotOverlap::Opaque,
            "replacing global Array means Array.prototype no longer identifies the literal-array iterator"
        );

        let iterator_next_slot = NativeWriteSlot {
            receiver: NativeReceiverPath::ArrayIteratorPrototype,
            key: NativePropertyKey::String("next".to_owned()),
        };
        let mut iterator_next_write = conditional_write(
            2,
            IntrinsicRoots::new(),
            IntrinsicRoots::new(),
            IntrinsicRoots::new(),
            false,
        )
        .witness;
        iterator_next_write
            .target_slots
            .insert(iterator_next_slot.clone());
        iterator_next_write.key_dependencies = IntrinsicRoots::from([
            NativeIntrinsic::Object,
            NativeIntrinsic::Array,
            NativeIntrinsic::Symbol,
        ]);
        assert_eq!(
            query(
                &iterator_next_write,
                &iterator_next_slot,
                &IntrinsicRoots::new(),
                false,
                true,
            ),
            NativeSlotOverlap::Known(SourceUnitOrdinal(2))
        );
        // Array may be invalidated by this very next write; the typed
        // Array-spread premise supplies just this receiver identity. Object
        // and Symbol remain independent and a global-object mutation defeats
        // both slot queries.
        assert_eq!(
            query(
                &iterator_next_write,
                &iterator_next_slot,
                &IntrinsicRoots::from([NativeIntrinsic::Array]),
                false,
                true,
            ),
            NativeSlotOverlap::Known(SourceUnitOrdinal(2))
        );
        for mutated in [
            IntrinsicRoots::from([NativeIntrinsic::Object]),
            IntrinsicRoots::from([NativeIntrinsic::Symbol]),
            IntrinsicRoots::from([NativeIntrinsic::GlobalObject]),
        ] {
            assert_eq!(
                query(
                    &iterator_next_write,
                    &iterator_next_slot,
                    &mutated,
                    false,
                    true,
                ),
                NativeSlotOverlap::Opaque
            );
        }
        assert_eq!(
            query(
                &iterator_next_write,
                &iterator_next_slot,
                &IntrinsicRoots::new(),
                false,
                false,
            ),
            NativeSlotOverlap::Opaque,
            "the typed premise cannot discharge a candidate not admitted by the query closure"
        );
        iterator_next_write
            .key_dependencies
            .insert(NativeIntrinsic::Math);
        assert_eq!(
            query(
                &iterator_next_write,
                &iterator_next_slot,
                &IntrinsicRoots::new(),
                false,
                true,
            ),
            NativeSlotOverlap::Opaque
        );
    }

    #[test]
    fn array_spread_protocol_view_keeps_candidate_effect_and_fallback_dependencies() {
        let candidate = array_iterator_next_candidate(1);
        let census = NativeWriteCensus {
            conditional: vec![candidate],
            ..NativeWriteCensus::default()
        };
        let protocol = resolve_array_spread_protocol_integrity([&census]);
        assert_eq!(
            protocol.mutated,
            IntrinsicRoots::from([NativeIntrinsic::Array])
        );
        assert!(
            !protocol.global_array_binding_mutated,
            "an iterator-prototype write is not a global Array binding write"
        );

        for independent_mutation in [
            NativeIntrinsic::Object,
            NativeIntrinsic::Symbol,
            NativeIntrinsic::GlobalObject,
        ] {
            let mut contradicted = census.clone();
            contradicted.unconditional.insert(independent_mutation);
            let protocol = resolve_array_spread_protocol_integrity([&contradicted]);
            assert!(
                protocol.mutated.contains(&NativeIntrinsic::GlobalObject),
                "candidate fallback must remain active when {independent_mutation:?} is mutated"
            );
        }

        let mut array_dependent = conditional_write(
            2,
            IntrinsicRoots::from([NativeIntrinsic::Array]),
            IntrinsicRoots::from([NativeIntrinsic::Promise]),
            IntrinsicRoots::from([NativeIntrinsic::GlobalObject]),
            false,
        );
        array_dependent
            .witness
            .target_slots
            .insert(NativeWriteSlot {
                receiver: NativeReceiverPath::Prototype(NativeIntrinsic::Array),
                key: NativePropertyKey::String("values".to_owned()),
            });
        let with_dependent = NativeWriteCensus {
            conditional: vec![array_iterator_next_candidate(1), array_dependent],
            ..NativeWriteCensus::default()
        };
        let protocol = resolve_array_spread_protocol_integrity([&with_dependent]);
        assert!(protocol.mutated.contains(&NativeIntrinsic::Array));
        assert!(
            protocol.mutated.contains(&NativeIntrinsic::GlobalObject),
            "the candidate's Array effect must still invalidate dependent writes"
        );

        let mut input_dependent = array_iterator_next_candidate(3);
        input_dependent
            .witness
            .value_input_refs
            .insert(analysis::analysis_inputs::ValueInputRef {
                defining_chunk: "test".to_owned(),
                input_index: 0,
            });
        let input_census = NativeWriteCensus {
            conditional: vec![input_dependent],
            ..NativeWriteCensus::default()
        };
        let protocol = resolve_array_spread_protocol_integrity([&input_census]);
        assert!(
            !protocol.mutated.contains(&NativeIntrinsic::Array),
            "author-conditioned candidate identity must not inherit the premise"
        );

        let mut computed_key_candidate = array_iterator_next_candidate(4);
        computed_key_candidate
            .dependencies
            .insert(NativeIntrinsic::Array);
        let original_refined = computed_key_candidate.refined.clone();
        let original_fallback = computed_key_candidate.fallback.clone();
        let computed_key_census = NativeWriteCensus {
            conditional: vec![computed_key_candidate],
            ..NativeWriteCensus::default()
        };
        let (query_census, authorized) = computed_key_census.for_array_spread_protocol_query();
        assert_eq!(
            query_census.conditional[0].dependencies,
            IntrinsicRoots::from([NativeIntrinsic::Array]),
            "the premise cannot remove an independently computed-key dependency"
        );
        assert_eq!(query_census.conditional[0].refined, original_refined);
        assert_eq!(query_census.conditional[0].fallback, original_fallback);
        let next_slot = NativeWriteSlot {
            receiver: NativeReceiverPath::ArrayIteratorPrototype,
            key: NativePropertyKey::String("next".to_owned()),
        };
        assert_eq!(
            array_spread_protocol_slot_overlap(
                std::iter::once(&computed_key_census.conditional[0].witness),
                &next_slot,
                &IntrinsicRoots::new(),
                false,
                &authorized,
                ArraySpreadNativeIteratorPremise,
            ),
            NativeSlotOverlap::Opaque,
            "merged key and receiver dependencies cannot authorize this computed key"
        );

        let mut global_array_write = conditional_write(
            5,
            IntrinsicRoots::new(),
            IntrinsicRoots::new(),
            IntrinsicRoots::new(),
            false,
        );
        global_array_write
            .witness
            .target_slots
            .insert(NativeWriteSlot {
                receiver: NativeReceiverPath::GlobalObject,
                key: NativePropertyKey::String("Array".to_owned()),
            });
        let mut array_prototype_write = conditional_write(
            6,
            IntrinsicRoots::new(),
            IntrinsicRoots::new(),
            IntrinsicRoots::new(),
            false,
        );
        array_prototype_write
            .witness
            .target_slots
            .insert(NativeWriteSlot {
                receiver: NativeReceiverPath::Prototype(NativeIntrinsic::Array),
                key: NativePropertyKey::SymbolIterator,
            });
        array_prototype_write
            .witness
            .key_dependencies
            .insert(NativeIntrinsic::Symbol);
        let global_replacement_census = NativeWriteCensus {
            conditional: vec![global_array_write, array_prototype_write],
            ..NativeWriteCensus::default()
        };
        let protocol = resolve_array_spread_protocol_integrity([&global_replacement_census]);
        assert!(protocol.global_array_binding_mutated);
        let array_iterator_slot = NativeWriteSlot {
            receiver: NativeReceiverPath::Prototype(NativeIntrinsic::Array),
            key: NativePropertyKey::SymbolIterator,
        };
        assert_eq!(
            array_spread_protocol_slot_overlap(
                std::iter::once(&global_replacement_census.conditional[1].witness),
                &array_iterator_slot,
                &protocol.mutated,
                protocol.global_array_binding_mutated,
                &protocol.premise_authorized_next_writes,
                ArraySpreadNativeIteratorPremise,
            ),
            NativeSlotOverlap::Opaque,
            "Array.prototype writes do not identify literal-array iterators after global Array replacement"
        );
    }

    #[test]
    fn invalidation_watchers_revisit_writes_and_preserve_witnesses() {
        let mut census = NativeWriteCensus::default();
        census.unconditional.insert(NativeIntrinsic::Set);
        // The first producer refines while Function is absent, then must run
        // again after the later producer contributes Function.
        census.conditional.push(conditional_write(
            0,
            BTreeSet::from([NativeIntrinsic::Function]),
            BTreeSet::from([NativeIntrinsic::Math]),
            BTreeSet::from([NativeIntrinsic::Array]),
            false,
        ));
        census.conditional.push(conditional_write(
            1,
            BTreeSet::from([NativeIntrinsic::Set]),
            BTreeSet::from([NativeIntrinsic::Promise]),
            BTreeSet::from([NativeIntrinsic::Function]),
            false,
        ));
        census.conditional.push(conditional_write(
            2,
            BTreeSet::from([NativeIntrinsic::Array]),
            BTreeSet::new(),
            BTreeSet::from([NativeIntrinsic::GlobalObject]),
            true,
        ));

        let integrity = resolve_native_integrity([&census]);

        assert_eq!(
            integrity.mutated,
            BTreeSet::from([
                NativeIntrinsic::Set,
                NativeIntrinsic::Function,
                NativeIntrinsic::Array,
                NativeIntrinsic::Math,
            ])
        );
        let revisited = integrity
            .witnesses
            .iter()
            .find(|witness| witness.statement_ordinal == SourceUnitOrdinal(0))
            .expect("conditional witness remains visible");
        assert_eq!(
            revisited.affected_roots,
            BTreeSet::from([NativeIntrinsic::Array])
        );
        assert_eq!(
            revisited.invalidated_key_dependencies,
            BTreeSet::from([NativeIntrinsic::Function])
        );
        let unreachable = integrity
            .witnesses
            .iter()
            .find(|witness| witness.statement_ordinal == SourceUnitOrdinal(2))
            .expect("unreachable write remains in provenance");
        assert!(unreachable.affected_roots.is_empty());
        assert!(!integrity.mutated.contains(&NativeIntrinsic::GlobalObject));
    }
}
