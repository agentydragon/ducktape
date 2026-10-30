//! Integrity flags for intrinsic slots.
//!
//! A11 supplies every slot's original identity. The closed-write census can
//! keep a slot `Intact`, or an exact-site `IntrinsicIntact` axiom can assert it
//! at one source site; otherwise the slot is unknown and its consumer falls
//! back to its opaque behavior. Rules and builtin rows name slots; nothing but
//! this module reads the census for them.

use crate::analysis_inputs::{NativeIntrinsic, NativeOperationInputRef, ProofDependencies};
use crate::reports::schema::{
    NativeMutationOperation, NativeMutationWitness, NativePropertyKey, NativeReceiverPath,
};
use std::collections::{BTreeMap, BTreeSet};

/// One intrinsic slot. Each belongs to a census family. `Global` and `Static`
/// slots are own data properties, unknown only when a census write can reach
/// that property (`OwnSlotWrites`); the prototype slots are unknown after any
/// write to their family or the global object.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum IntrinsicSlot {
    /// The global object's own data property naming a family's constructor or
    /// namespace: `%Set%`, `%Math%`.
    Global(NativeIntrinsic),
    /// A data property of that constructor or namespace: `%Object.freeze%`,
    /// `%Symbol.for%`.
    Static(NativeIntrinsic, &'static str),
    /// The family's prototype object with every property on it:
    /// `%Array.prototype%`, `%Set.prototype.add%`.
    Prototype(NativeIntrinsic),
    /// `%ArrayIteratorPrototype%` and its `next`.
    ArrayIteratorPrototype,
    /// `%SetIteratorPrototype%` and its `next`.
    SetIteratorPrototype,
}

impl IntrinsicSlot {
    /// Every slot a computed key's `key_dependencies` family was read through:
    /// `Symbol.iterator` and `Symbol.for(...)` keys, `Object.entries(...)`
    /// keys destructured through the Array iterator, and the
    /// `Object.getPrototypeOf` path to an iterator prototype. `None` for a
    /// family no key derivation reads.
    fn read_by_key_dependency(family: NativeIntrinsic) -> Option<Vec<Self>> {
        Some(match family {
            NativeIntrinsic::Symbol => vec![
                Self::Global(family),
                Self::Static(family, "iterator"),
                Self::Static(family, "for"),
            ],
            NativeIntrinsic::Object => vec![
                Self::Global(family),
                Self::Static(family, "entries"),
                Self::Static(family, "getPrototypeOf"),
            ],
            NativeIntrinsic::Array => vec![Self::Prototype(family), Self::ArrayIteratorPrototype],
            NativeIntrinsic::Set => vec![Self::Prototype(family), Self::SetIteratorPrototype],
            _ => return None,
        })
    }

    /// The census family whose writes the slot inherits.
    pub fn family(self) -> NativeIntrinsic {
        match self {
            Self::Global(family) | Self::Static(family, _) | Self::Prototype(family) => family,
            Self::ArrayIteratorPrototype => NativeIntrinsic::Array,
            Self::SetIteratorPrototype => NativeIntrinsic::Set,
        }
    }
}

/// What a consumer may assume about a group of slots.
#[derive(Debug, Clone, Copy)]
pub(crate) enum Integrity<'a> {
    /// The closed-write census proves the slots, consuming these captured-value
    /// refinements.
    Intact(&'a ProofDependencies),
    /// An exact-site author axiom covers every slot.
    AuthorAsserted(&'a NativeOperationInputRef),
    Unknown,
}

impl Integrity<'_> {
    /// The typed premise a consumer records.
    pub(crate) fn dependencies(self) -> Option<ProofDependencies> {
        match self {
            Self::Intact(dependencies) => Some(dependencies.clone()),
            Self::AuthorAsserted(reference) => Some(ProofDependencies {
                native_operation_inputs: BTreeSet::from([reference.clone()]),
                ..ProofDependencies::default()
            }),
            Self::Unknown => None,
        }
    }
}

/// `IntrinsicIntact(slots, site)`: one validated native input asserts these
/// slots at its exact source site, keyed by the site's start offset.
#[derive(Debug, Clone)]
pub(crate) struct IntrinsicIntact {
    pub(crate) slots: BTreeSet<IntrinsicSlot>,
    pub(crate) reference: NativeOperationInputRef,
}

/// Possible writes to `%Object.prototype%` properties, from the separate
/// prototype census: it also sees descriptor capabilities escaping into calls
/// and reflective writes, which the family census does not.
#[derive(Debug, Clone, Default)]
pub(crate) struct PrototypeWrites {
    /// Writes whose key is unknown.
    any_key: usize,
    /// Writes per known string key.
    by_key: BTreeMap<String, usize>,
}

impl PrototypeWrites {
    /// One entry per write: its finite key set, or `None` for any key.
    pub(crate) fn new<'a>(writes: impl IntoIterator<Item = Option<&'a BTreeSet<String>>>) -> Self {
        let mut counts = Self::default();
        for keys in writes {
            match keys {
                Some(keys) => {
                    for key in keys {
                        *counts.by_key.entry(key.clone()).or_default() += 1;
                    }
                }
                None => counts.any_key += 1,
            }
        }
        counts
    }

    fn blocking(&self, property: &str) -> usize {
        self.any_key + self.by_key.get(property).copied().unwrap_or(0)
    }
}

/// The own data properties of the global object and of each census
/// constructor or namespace that some census write can reach. A write reaches
/// only the receiver's own property under its key: a setter or Proxy trap it
/// runs is source code with census writes of its own, and A11 excludes host
/// code mutating intrinsics. So a key reaches only same-named slots, an exact
/// global-object receiver only `Global` slots, and an exact constructor
/// receiver only that constructor's `Static` slots.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
struct OwnSlotWrites {
    /// An unknown key on an unknown receiver.
    every_slot: bool,
    /// An unknown key on the global object.
    every_global: bool,
    global_keys: BTreeSet<String>,
    /// Unknown keys on these constructors.
    every_static: BTreeSet<NativeIntrinsic>,
    static_keys: BTreeSet<(NativeIntrinsic, String)>,
    /// Keys on a receiver that may be any native object.
    any_receiver_keys: BTreeSet<String>,
}

impl OwnSlotWrites {
    /// The least fixed point over the reachable witnesses. A constructor or
    /// prototype path names the object its source binding holds, so it is
    /// exact only while that `Global` slot is intact; a computed key or an
    /// iterator-prototype path is exact only while the slots it was derived
    /// through are intact. A family root without a witness reaches every own
    /// slot of the family, or of every family for the global object.
    fn resolve(
        witnesses: &[NativeMutationWitness],
        mutated: &BTreeSet<NativeIntrinsic>,
        prototype_intact: impl Fn(IntrinsicSlot) -> bool,
    ) -> Self {
        let witnesses: Vec<_> = witnesses
            .iter()
            .filter(|witness| {
                witness.unreachable_branch.is_none() && !witness.affected_roots.is_empty()
            })
            .collect();
        let witnessed: BTreeSet<_> = witnesses
            .iter()
            .flat_map(|witness| witness.affected_roots.iter().copied())
            .collect();
        let mut base = Self::default();
        for root in mutated.difference(&witnessed) {
            match root.global_binding() {
                Some(name) => {
                    base.global_keys.insert(name.to_owned());
                    base.every_static.insert(*root);
                }
                None => base.every_slot = true,
            }
        }
        let mut writes = base.clone();
        loop {
            let mut next = base.clone();
            for witness in &witnesses {
                next.add(witness, &writes, &prototype_intact);
            }
            if next == writes {
                return writes;
            }
            writes = next;
        }
    }

    fn add(
        &mut self,
        witness: &NativeMutationWitness,
        current: &Self,
        prototype_intact: impl Fn(IntrinsicSlot) -> bool,
    ) {
        if witness.operation == NativeMutationOperation::BindingAssignment {
            // An assignment to the unqualified global binding, recorded even
            // where a local declaration may shadow it. Rebinding a
            // global-object alias makes its member writes any-receiver.
            for root in &witness.affected_roots {
                match root.global_binding() {
                    Some(name) => {
                        self.global_keys.insert(name.to_owned());
                    }
                    None => self.every_slot = true,
                }
            }
            return;
        }
        if witness.target_slots.is_empty() {
            self.every_slot = true;
            return;
        }
        let derivation_intact = witness.key_dependencies.iter().all(|family| {
            IntrinsicSlot::read_by_key_dependency(*family).is_some_and(|slots| {
                slots.into_iter().all(|slot| match slot {
                    IntrinsicSlot::Global(_) | IntrinsicSlot::Static(..) => current.intact(slot),
                    _ => prototype_intact(slot),
                })
            })
        });
        for target in &witness.target_slots {
            let key = if derivation_intact {
                &target.key
            } else {
                &NativePropertyKey::Unknown
            };
            let key = match key {
                NativePropertyKey::String(key) => Some(key),
                NativePropertyKey::Unknown => None,
                // Every own slot is string-keyed.
                NativePropertyKey::SymbolIterator | NativePropertyKey::OtherSymbol => continue,
            };
            match &target.receiver {
                NativeReceiverPath::GlobalObject => match key {
                    Some(key) => {
                        self.global_keys.insert(key.clone());
                    }
                    None => self.every_global = true,
                },
                NativeReceiverPath::Constructor(family)
                    if current.intact(IntrinsicSlot::Global(*family)) =>
                {
                    match key {
                        Some(key) => {
                            self.static_keys.insert((*family, key.clone()));
                        }
                        None => {
                            self.every_static.insert(*family);
                            // An update of the binding itself (`Map++`) has
                            // this path too.
                            if witness.operation == NativeMutationOperation::Update {
                                self.global_keys
                                    .extend(family.global_binding().map(str::to_owned));
                            }
                        }
                    }
                }
                // `%F.prototype%` and the iterator prototypes own no
                // `Global` or `Static` slot.
                NativeReceiverPath::Prototype(family)
                    if current.intact(IntrinsicSlot::Global(*family)) => {}
                NativeReceiverPath::ArrayIteratorPrototype
                | NativeReceiverPath::SetIteratorPrototype
                    if derivation_intact => {}
                _ => match key {
                    Some(key) => {
                        self.any_receiver_keys.insert(key.clone());
                    }
                    None => self.every_slot = true,
                },
            }
        }
    }

    /// Whether no write reaches this `Global` or `Static` slot.
    fn intact(&self, slot: IntrinsicSlot) -> bool {
        if self.every_slot {
            return false;
        }
        match slot {
            IntrinsicSlot::Global(family) => family.global_binding().is_some_and(|name| {
                !self.every_global
                    && !self.global_keys.contains(name)
                    && !self.any_receiver_keys.contains(name)
            }),
            IntrinsicSlot::Static(family, property) => {
                !self.every_static.contains(&family)
                    && !self.static_keys.contains(&(family, property.to_owned()))
                    && !self.any_receiver_keys.contains(property)
            }
            _ => unreachable!("own-slot query for {slot:?}"),
        }
    }

    /// Whether the slot is unknown through a write whose receiver may be the
    /// global object or any native object.
    fn reached_through_global(&self, slot: IntrinsicSlot) -> bool {
        self.every_slot
            || match slot {
                IntrinsicSlot::Global(family) => {
                    self.every_global
                        || family
                            .global_binding()
                            .is_some_and(|name| self.any_receiver_keys.contains(name))
                }
                IntrinsicSlot::Static(_, property) => self.any_receiver_keys.contains(property),
                _ => false,
            }
    }
}

/// The completed census as slot flags, plus the axioms resolved from validated
/// author inputs. The default answers `Unknown` for every census query.
#[derive(Debug, Clone, Default)]
pub(crate) struct IntegrityFlags {
    /// Every analyzed chunk contributed its writes and no dynamic scope can
    /// hide one.
    census_closed: bool,
    mutated: BTreeSet<NativeIntrinsic>,
    own_slot_writes: OwnSlotWrites,
    census_dependencies: ProofDependencies,
    axioms: BTreeMap<u32, IntrinsicIntact>,
    prototype_writes: PrototypeWrites,
}

impl IntegrityFlags {
    /// `witnesses` are the completed census's, from every analyzed chunk.
    pub(crate) fn new(
        census_closed: bool,
        mutated: BTreeSet<NativeIntrinsic>,
        witnesses: &[NativeMutationWitness],
        census_dependencies: ProofDependencies,
        axioms: BTreeMap<u32, IntrinsicIntact>,
        prototype_writes: PrototypeWrites,
    ) -> Self {
        let own_slot_writes = OwnSlotWrites::resolve(witnesses, &mutated, |slot| {
            prototype_slot_intact(&mutated, slot)
        });
        Self {
            census_closed,
            mutated,
            own_slot_writes,
            census_dependencies,
            axioms,
            prototype_writes,
        }
    }

    /// The completed census's own refinements, for a rule that checks exact
    /// slots against the raw write census itself.
    pub(crate) fn closed_census(&self) -> Option<&ProofDependencies> {
        self.census_closed.then_some(&self.census_dependencies)
    }

    /// An own data slot is intact when no census write reaches it. A
    /// prototype slot covers every property on the object, so it stays
    /// family-level: intact only when neither its family nor the global
    /// object has a known or possible write.
    fn census_intact(&self, slot: IntrinsicSlot) -> bool {
        self.census_closed
            && match slot {
                IntrinsicSlot::Global(_) | IntrinsicSlot::Static(..) => {
                    self.own_slot_writes.intact(slot)
                }
                _ => prototype_slot_intact(&self.mutated, slot),
            }
    }

    /// All `slots` at `site`. An axiom at the site is consumed when it covers
    /// every slot, so the proof names the premise the site was audited under;
    /// otherwise every slot must be census-intact. Mixed coverage is unknown.
    pub(crate) fn require(&self, slots: &[IntrinsicSlot], site: Option<u32>) -> Integrity<'_> {
        if let Some(axiom) = site.and_then(|site| self.axioms.get(&site))
            && slots.iter().all(|slot| axiom.slots.contains(slot))
        {
            return Integrity::AuthorAsserted(&axiom.reference);
        }
        if slots.iter().all(|slot| self.census_intact(*slot)) {
            Integrity::Intact(&self.census_dependencies)
        } else {
            Integrity::Unknown
        }
    }

    /// `%Object.prototype%[property]` is absent unless a possible write names
    /// it. **Deviation** from family inheritance: ordinary literals reach
    /// `%Object.prototype%` without the global `Object` binding, so writes to
    /// the `Object` constructor or other global slots do not touch it. Returns
    /// whether the property is intact and how many writes block it.
    pub(crate) fn object_prototype_property(&self, property: &str) -> (bool, usize) {
        let blocking = self.prototype_writes.blocking(property);
        (self.census_closed && blocking == 0, blocking)
    }

    /// The census families (and the global object) that make `slots` unknown.
    pub(crate) fn mutated_roots(&self, slots: &[IntrinsicSlot]) -> BTreeSet<NativeIntrinsic> {
        let mut roots = BTreeSet::new();
        for slot in slots {
            match slot {
                IntrinsicSlot::Global(_) | IntrinsicSlot::Static(..) => {
                    if !self.own_slot_writes.intact(*slot) {
                        roots.insert(if self.own_slot_writes.reached_through_global(*slot) {
                            NativeIntrinsic::GlobalObject
                        } else {
                            slot.family()
                        });
                    }
                }
                _ => roots.extend(
                    [slot.family(), NativeIntrinsic::GlobalObject]
                        .into_iter()
                        .filter(|root| self.mutated.contains(root)),
                ),
            }
        }
        roots
    }
}

fn prototype_slot_intact(mutated: &BTreeSet<NativeIntrinsic>, slot: IntrinsicSlot) -> bool {
    !mutated.contains(&NativeIntrinsic::GlobalObject) && !mutated.contains(&slot.family())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn reference() -> NativeOperationInputRef {
        NativeOperationInputRef {
            defining_chunk: "fixture".to_owned(),
            input_index: 0,
        }
    }

    /// Exhaustive over families: without a witness naming the write, a slot
    /// is intact exactly when the census is closed and neither its family nor
    /// the global object is mutated.
    #[test]
    fn unwitnessed_roots_reach_their_whole_family() {
        let families = [
            NativeIntrinsic::Object,
            NativeIntrinsic::Array,
            NativeIntrinsic::Set,
            NativeIntrinsic::Symbol,
        ];
        let slots = [
            IntrinsicSlot::Global(NativeIntrinsic::Set),
            IntrinsicSlot::Static(NativeIntrinsic::Object, "freeze"),
            IntrinsicSlot::Prototype(NativeIntrinsic::Array),
            IntrinsicSlot::ArrayIteratorPrototype,
            IntrinsicSlot::SetIteratorPrototype,
        ];
        for mutated in families
            .iter()
            .map(|family| BTreeSet::from([*family]))
            .chain([
                BTreeSet::new(),
                BTreeSet::from([NativeIntrinsic::GlobalObject]),
            ])
        {
            let flags = IntegrityFlags::new(
                true,
                mutated.clone(),
                &[],
                ProofDependencies::default(),
                BTreeMap::new(),
                PrototypeWrites::default(),
            );
            for slot in slots {
                let intact = matches!(flags.require(&[slot], None), Integrity::Intact(_));
                assert_eq!(
                    intact,
                    !mutated.contains(&slot.family())
                        && !mutated.contains(&NativeIntrinsic::GlobalObject),
                    "{slot:?} under {mutated:?}"
                );
            }
        }
        let open = IntegrityFlags::default();
        assert!(matches!(
            open.require(&[slots[0]], None),
            Integrity::Unknown
        ));
    }

    #[test]
    fn an_axiom_covers_only_its_slots_at_its_site() {
        let site = 3;
        let flags = IntegrityFlags::new(
            false,
            BTreeSet::new(),
            &[],
            ProofDependencies::default(),
            BTreeMap::from([(
                3,
                IntrinsicIntact {
                    slots: BTreeSet::from([IntrinsicSlot::Global(NativeIntrinsic::Set)]),
                    reference: reference(),
                },
            )]),
            PrototypeWrites::default(),
        );
        let set = [IntrinsicSlot::Global(NativeIntrinsic::Set)];
        assert!(matches!(
            flags.require(&set, Some(site)),
            Integrity::AuthorAsserted(_)
        ));
        assert!(matches!(flags.require(&set, None), Integrity::Unknown));
        assert!(matches!(
            flags.require(
                &[set[0], IntrinsicSlot::Prototype(NativeIntrinsic::Set)],
                Some(site)
            ),
            Integrity::Unknown
        ));
    }

    fn witness(
        root: NativeIntrinsic,
        receiver: NativeReceiverPath,
        key: NativePropertyKey,
        key_dependencies: &[NativeIntrinsic],
    ) -> NativeMutationWitness {
        NativeMutationWitness {
            chunk_id: "fixture".to_owned(),
            statement_ordinal: source_identity::SourceUnitOrdinal(0),
            source_location: None,
            operation: NativeMutationOperation::PropertyAssignment,
            receiver_roots: BTreeSet::from([root]),
            through_local_alias: false,
            affected_roots: BTreeSet::from([root]),
            target_slots: BTreeSet::from([crate::reports::schema::NativeWriteSlot {
                receiver,
                key,
            }]),
            key_dependencies: key_dependencies.iter().copied().collect(),
            invalidated_key_dependencies: BTreeSet::new(),
            value_input_refs: BTreeSet::new(),
            value_input_evidence: Vec::new(),
            unreachable_branch: None,
            span: swc_common::DUMMY_SP,
        }
    }

    fn string(key: &str) -> NativePropertyKey {
        NativePropertyKey::String(key.to_owned())
    }

    /// The intact slots among `probes` under these witnesses, with every
    /// witnessed root mutated as the family census records it.
    fn intact(witnesses: &[NativeMutationWitness], probes: &[IntrinsicSlot]) -> Vec<IntrinsicSlot> {
        let mutated = witnesses
            .iter()
            .flat_map(|witness| witness.affected_roots.iter().copied())
            .collect();
        let flags = IntegrityFlags::new(
            true,
            mutated,
            witnesses,
            ProofDependencies::default(),
            BTreeMap::new(),
            PrototypeWrites::default(),
        );
        probes
            .iter()
            .copied()
            .filter(|slot| matches!(flags.require(&[*slot], None), Integrity::Intact(_)))
            .collect()
    }

    const MAP: IntrinsicSlot = IntrinsicSlot::Global(NativeIntrinsic::Map);
    const OBJECT: IntrinsicSlot = IntrinsicSlot::Global(NativeIntrinsic::Object);
    const KEYS: IntrinsicSlot = IntrinsicSlot::Static(NativeIntrinsic::Object, "keys");
    const ENTRIES: IntrinsicSlot = IntrinsicSlot::Static(NativeIntrinsic::Object, "entries");
    const SYMBOL_FOR: IntrinsicSlot = IntrinsicSlot::Static(NativeIntrinsic::Symbol, "for");
    const OBJECT_PROTOTYPE: IntrinsicSlot = IntrinsicSlot::Prototype(NativeIntrinsic::Object);
    const PROBES: &[IntrinsicSlot] = &[MAP, OBJECT, KEYS, ENTRIES, SYMBOL_FOR, OBJECT_PROTOTYPE];

    #[test]
    fn a_known_key_reaches_only_same_named_own_slots() {
        let unknown = |key| {
            witness(
                NativeIntrinsic::GlobalObject,
                NativeReceiverPath::Unknown,
                string(key),
                &[],
            )
        };
        assert_eq!(
            intact(&[unknown("href")], PROBES),
            [MAP, OBJECT, KEYS, ENTRIES, SYMBOL_FOR]
        );
        assert_eq!(
            intact(&[unknown("keys")], PROBES),
            [MAP, OBJECT, ENTRIES, SYMBOL_FOR]
        );
        assert_eq!(
            intact(&[unknown("Map")], PROBES),
            [OBJECT, KEYS, ENTRIES, SYMBOL_FOR]
        );
    }

    #[test]
    fn exact_receivers_reach_only_their_own_slots() {
        let global = |key| {
            witness(
                NativeIntrinsic::GlobalObject,
                NativeReceiverPath::GlobalObject,
                key,
                &[],
            )
        };
        assert_eq!(
            intact(&[global(string("keys"))], PROBES),
            [MAP, OBJECT, KEYS, ENTRIES, SYMBOL_FOR]
        );
        assert_eq!(
            intact(&[global(NativePropertyKey::Unknown)], PROBES),
            [KEYS, ENTRIES, SYMBOL_FOR]
        );
        let object = |key| {
            witness(
                NativeIntrinsic::Object,
                NativeReceiverPath::Constructor(NativeIntrinsic::Object),
                key,
                &[],
            )
        };
        assert_eq!(
            intact(&[object(string("entries"))], PROBES),
            [MAP, OBJECT, KEYS, SYMBOL_FOR]
        );
        assert_eq!(
            intact(&[object(NativePropertyKey::Unknown)], PROBES),
            [MAP, OBJECT, SYMBOL_FOR]
        );
        // An update may rebind the binding (`Object++`), after which the
        // path names an unknown receiver.
        let mut update = object(NativePropertyKey::Unknown);
        update.operation = NativeMutationOperation::Update;
        assert!(intact(&[update], PROBES).is_empty());
        // `%Object.prototype%` owns no own data slot here.
        let prototype = witness(
            NativeIntrinsic::Object,
            NativeReceiverPath::Prototype(NativeIntrinsic::Object),
            NativePropertyKey::Unknown,
            &[],
        );
        assert_eq!(
            intact(&[prototype], PROBES),
            [MAP, OBJECT, KEYS, ENTRIES, SYMBOL_FOR]
        );
    }

    #[test]
    fn a_replaced_binding_makes_its_paths_any_receiver() {
        let rebind = witness(
            NativeIntrinsic::Object,
            NativeReceiverPath::GlobalObject,
            string("Object"),
            &[],
        );
        let entries = witness(
            NativeIntrinsic::Object,
            NativeReceiverPath::Constructor(NativeIntrinsic::Object),
            string("entries"),
            &[],
        );
        assert_eq!(
            intact(&[entries.clone()], PROBES),
            [MAP, OBJECT, KEYS, SYMBOL_FOR]
        );
        // `Object.entries = f` after `Object = other` may write any object's
        // `entries`; it no longer reaches `keys` either way.
        assert_eq!(intact(&[rebind, entries], PROBES), [MAP, KEYS, SYMBOL_FOR]);
    }

    #[test]
    fn a_computed_key_is_exact_only_while_its_derivation_is_intact() {
        let symbol_keyed = witness(
            NativeIntrinsic::GlobalObject,
            NativeReceiverPath::GlobalObject,
            NativePropertyKey::OtherSymbol,
            &[NativeIntrinsic::Symbol],
        );
        assert_eq!(
            intact(&[symbol_keyed.clone()], PROBES),
            [MAP, OBJECT, KEYS, ENTRIES, SYMBOL_FOR]
        );
        let replaced_for = witness(
            NativeIntrinsic::Symbol,
            NativeReceiverPath::Constructor(NativeIntrinsic::Symbol),
            string("for"),
            &[],
        );
        // With `Symbol.for` replaced the key may be any string.
        assert_eq!(
            intact(&[symbol_keyed, replaced_for], PROBES),
            [KEYS, ENTRIES]
        );
    }

    #[test]
    fn a_binding_assignment_reaches_only_its_global_slot() {
        let mut rebind = witness(
            NativeIntrinsic::Map,
            NativeReceiverPath::GlobalObject,
            string("Map"),
            &[],
        );
        rebind.operation = NativeMutationOperation::BindingAssignment;
        // The census leaves an assignment's target slot empty.
        rebind.target_slots.clear();
        assert_eq!(
            intact(&[rebind], PROBES),
            [OBJECT, KEYS, ENTRIES, SYMBOL_FOR, OBJECT_PROTOTYPE]
        );
    }
}
