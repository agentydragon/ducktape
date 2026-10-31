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
use std::fmt;

/// One intrinsic slot. Each belongs to a census family. `Global`, `Static`,
/// `PrototypeProperty` and `Species` slots are own properties, unknown only
/// when a census write can reach that property (`OwnSlotWrites`); the
/// whole-object prototype slots are unknown after any write to their family
/// or the global object.
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
    /// One string-keyed own property of the family's prototype object:
    /// `%Array.prototype.map%`, `%Array.prototype.constructor%`.
    PrototypeProperty(NativeIntrinsic, &'static str),
    /// The `@@species` accessor of the family's constructor. Symbol keys are
    /// not told apart: any Symbol- or unknown-keyed write that may land on
    /// the constructor reaches it.
    Species(NativeIntrinsic),
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
            Self::Global(family)
            | Self::Static(family, _)
            | Self::Prototype(family)
            | Self::PrototypeProperty(family, _)
            | Self::Species(family) => family,
            Self::ArrayIteratorPrototype => NativeIntrinsic::Array,
            Self::SetIteratorPrototype => NativeIntrinsic::Set,
        }
    }
}

impl fmt::Display for IntrinsicSlot {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        SlotName::from(*self).fmt(f)
    }
}

/// An [`IntrinsicSlot`] whose static property name is owned, as reports and
/// queries spell it: `%Set%`, `%Object.freeze%`, `%Array.prototype%`,
/// `%ArrayIteratorPrototype%`.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub enum SlotName {
    Global(NativeIntrinsic),
    Static(NativeIntrinsic, String),
    Prototype(NativeIntrinsic),
    ArrayIteratorPrototype,
    SetIteratorPrototype,
    PrototypeProperty(NativeIntrinsic, String),
    Species(NativeIntrinsic),
}

impl SlotName {
    pub fn family(&self) -> NativeIntrinsic {
        match self {
            Self::Global(family)
            | Self::Static(family, _)
            | Self::Prototype(family)
            | Self::PrototypeProperty(family, _)
            | Self::Species(family) => *family,
            Self::ArrayIteratorPrototype => NativeIntrinsic::Array,
            Self::SetIteratorPrototype => NativeIntrinsic::Set,
        }
    }

    fn own(&self) -> Option<OwnSlot<'_>> {
        match self {
            Self::Global(family) => Some(OwnSlot::Global(*family)),
            Self::Static(family, property) => Some(OwnSlot::Static(*family, property)),
            Self::PrototypeProperty(family, property) => {
                Some(OwnSlot::PrototypeProperty(*family, property))
            }
            Self::Species(family) => Some(OwnSlot::Species(*family)),
            _ => None,
        }
    }

    /// The census receiver path naming this prototype slot's object.
    fn prototype_receiver(&self) -> Option<NativeReceiverPath> {
        match self {
            Self::Prototype(family) => Some(NativeReceiverPath::Prototype(*family)),
            Self::ArrayIteratorPrototype => Some(NativeReceiverPath::ArrayIteratorPrototype),
            Self::SetIteratorPrototype => Some(NativeReceiverPath::SetIteratorPrototype),
            Self::Global(_) | Self::Static(..) | Self::PrototypeProperty(..) | Self::Species(_) => {
                None
            }
        }
    }
}

impl From<IntrinsicSlot> for SlotName {
    fn from(slot: IntrinsicSlot) -> Self {
        match slot {
            IntrinsicSlot::Global(family) => Self::Global(family),
            IntrinsicSlot::Static(family, property) => Self::Static(family, property.to_owned()),
            IntrinsicSlot::Prototype(family) => Self::Prototype(family),
            IntrinsicSlot::ArrayIteratorPrototype => Self::ArrayIteratorPrototype,
            IntrinsicSlot::SetIteratorPrototype => Self::SetIteratorPrototype,
            IntrinsicSlot::PrototypeProperty(family, property) => {
                Self::PrototypeProperty(family, property.to_owned())
            }
            IntrinsicSlot::Species(family) => Self::Species(family),
        }
    }
}

impl fmt::Display for SlotName {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let name = |family: &NativeIntrinsic| family.global_binding().unwrap_or("?");
        match self {
            Self::Global(family) => write!(f, "%{}%", name(family)),
            Self::Static(family, property) => write!(f, "%{}.{property}%", name(family)),
            Self::Prototype(family) => write!(f, "%{}.prototype%", name(family)),
            Self::ArrayIteratorPrototype => f.write_str("%ArrayIteratorPrototype%"),
            Self::SetIteratorPrototype => f.write_str("%SetIteratorPrototype%"),
            Self::PrototypeProperty(family, property) => {
                write!(f, "%{}.prototype.{property}%", name(family))
            }
            Self::Species(family) => write!(f, "%{}[@@species]%", name(family)),
        }
    }
}

impl std::str::FromStr for SlotName {
    type Err = String;

    /// Accepts the display form with or without `%`.
    fn from_str(text: &str) -> Result<Self, Self::Err> {
        let name = text.trim().trim_matches('%');
        let (name, species) = match name.strip_suffix("[@@species]") {
            Some(name) => (name, true),
            None => (name, false),
        };
        match name {
            "ArrayIteratorPrototype" => return Ok(Self::ArrayIteratorPrototype),
            "SetIteratorPrototype" => return Ok(Self::SetIteratorPrototype),
            _ => {}
        }
        let (global, member) = match name.split_once('.') {
            Some((global, member)) => (global, Some(member)),
            None => (name, None),
        };
        let family = NativeIntrinsic::for_global_binding(global)
            .filter(|family| family.global_binding().is_some())
            .ok_or_else(|| {
                format!(
                    "{text:?} names no intrinsic slot; expected Family, Family.member, \
                     Family.prototype, Family.prototype.member, Family[@@species], \
                     ArrayIteratorPrototype or SetIteratorPrototype for a census family ({})",
                    NativeIntrinsic::GLOBAL_BINDINGS
                        .iter()
                        .filter(|binding| NativeIntrinsic::for_global_binding(binding)
                            .is_some_and(|family| family.global_binding().is_some()))
                        .copied()
                        .collect::<Vec<_>>()
                        .join(", ")
                )
            })?;
        Ok(match member {
            None if species => Self::Species(family),
            _ if species => return Err(format!("{text:?}: @@species follows a family name")),
            None => Self::Global(family),
            Some("prototype") => Self::Prototype(family),
            Some(member) => match member.strip_prefix("prototype.") {
                Some(property) => Self::PrototypeProperty(family, property.to_owned()),
                None => Self::Static(family, member.to_owned()),
            },
        })
    }
}

/// An own-property slot with a borrowed property name.
#[derive(Debug, Clone, Copy)]
enum OwnSlot<'a> {
    Global(NativeIntrinsic),
    Static(NativeIntrinsic, &'a str),
    PrototypeProperty(NativeIntrinsic, &'a str),
    Species(NativeIntrinsic),
}

impl OwnSlot<'static> {
    fn of(slot: IntrinsicSlot) -> Self {
        match slot {
            IntrinsicSlot::Global(family) => Self::Global(family),
            IntrinsicSlot::Static(family, property) => Self::Static(family, property),
            IntrinsicSlot::PrototypeProperty(family, property) => {
                Self::PrototypeProperty(family, property)
            }
            IntrinsicSlot::Species(family) => Self::Species(family),
            _ => unreachable!("own-slot query for {slot:?}"),
        }
    }
}

impl OwnSlot<'_> {
    fn family(self) -> NativeIntrinsic {
        match self {
            Self::Global(family)
            | Self::Static(family, _)
            | Self::PrototypeProperty(family, _)
            | Self::Species(family) => family,
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
/// code mutating intrinsics. So a key reaches only slots its domain may name,
/// an exact global-object receiver only `Global` slots, and an exact
/// constructor receiver only that constructor's `Static` slots. Keys are
/// string domains (`String`, `Affixed`) or `Unknown`. Symbol keys name only
/// `Species` slots, recorded apart.
///
/// The same holds for string-keyed properties of a prototype object
/// (`PrototypeProperty`): an exact `%F.prototype%` receiver reaches only that
/// prototype's properties under its key. A write to the global object or to
/// a constructor names a different object and does not reach them.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
struct OwnSlotWrites {
    /// An unknown key on an unknown receiver.
    every_slot: bool,
    global_keys: BTreeSet<NativePropertyKey>,
    static_keys: BTreeSet<(NativeIntrinsic, NativePropertyKey)>,
    prototype_keys: BTreeSet<(NativeIntrinsic, NativePropertyKey)>,
    /// Constructors with a Symbol-keyed write on their exact receiver.
    symbol_statics: BTreeSet<NativeIntrinsic>,
    /// Keys on a receiver that may be any native object.
    any_receiver_keys: BTreeSet<NativePropertyKey>,
    /// A Symbol key on a receiver that may be any native object.
    any_receiver_symbol: bool,
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
                    base.global_keys
                        .insert(NativePropertyKey::String(name.to_owned()));
                    base.static_keys.insert((*root, NativePropertyKey::Unknown));
                    base.prototype_keys
                        .insert((*root, NativePropertyKey::Unknown));
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
                        self.global_keys
                            .insert(NativePropertyKey::String(name.to_owned()));
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
                target.key.clone()
            } else {
                NativePropertyKey::Unknown
            };
            // Only `Species` slots are Symbol-keyed.
            if matches!(
                key,
                NativePropertyKey::SymbolIterator | NativePropertyKey::OtherSymbol
            ) {
                match &target.receiver {
                    NativeReceiverPath::Constructor(family)
                        if current.intact(IntrinsicSlot::Global(*family)) =>
                    {
                        self.symbol_statics.insert(*family);
                    }
                    NativeReceiverPath::GlobalObject => {}
                    NativeReceiverPath::Prototype(family)
                        if current.intact(IntrinsicSlot::Global(*family)) => {}
                    NativeReceiverPath::ArrayIteratorPrototype
                    | NativeReceiverPath::SetIteratorPrototype
                        if derivation_intact => {}
                    _ => self.any_receiver_symbol = true,
                }
                continue;
            }
            match &target.receiver {
                NativeReceiverPath::GlobalObject => {
                    self.global_keys.insert(key);
                }
                NativeReceiverPath::Constructor(family)
                    if current.intact(IntrinsicSlot::Global(*family)) =>
                {
                    // An update of the binding itself (`Map++`) has this
                    // keyless path too.
                    if key == NativePropertyKey::Unknown
                        && witness.operation == NativeMutationOperation::Update
                    {
                        self.global_keys.extend(
                            family
                                .global_binding()
                                .map(|name| NativePropertyKey::String(name.to_owned())),
                        );
                    }
                    self.static_keys.insert((*family, key));
                }
                NativeReceiverPath::Prototype(family)
                    if current.intact(IntrinsicSlot::Global(*family)) =>
                {
                    self.prototype_keys.insert((*family, key));
                }
                // The iterator prototypes own no recorded slot.
                NativeReceiverPath::ArrayIteratorPrototype
                | NativeReceiverPath::SetIteratorPrototype
                    if derivation_intact => {}
                _ if key == NativePropertyKey::Unknown => self.every_slot = true,
                _ => {
                    self.any_receiver_keys.insert(key);
                }
            }
        }
    }

    /// Whether no write reaches this `Global` or `Static` slot.
    fn intact(&self, slot: IntrinsicSlot) -> bool {
        self.own_intact(OwnSlot::of(slot))
    }

    fn own_intact(&self, slot: OwnSlot<'_>) -> bool {
        !self.every_slot && !self.reached_by_key(slot, true)
    }

    /// The recorded keys on the slot's exact receiver and on any receiver
    /// that may name it.
    fn keys_naming<'s>(
        &'s self,
        slot: OwnSlot<'s>,
    ) -> (
        impl Iterator<Item = &'s NativePropertyKey>,
        impl Iterator<Item = &'s NativePropertyKey>,
    ) {
        let name = match slot {
            OwnSlot::Global(family) => family.global_binding(),
            OwnSlot::Static(_, property) | OwnSlot::PrototypeProperty(_, property) => {
                Some(property)
            }
            OwnSlot::Species(_) => unreachable!("Species slots are Symbol-keyed"),
        };
        let may_name =
            move |key: &NativePropertyKey| name.is_none_or(|name| key.may_be_string(name));
        let exact = self
            .global_keys
            .iter()
            .filter(move |_| matches!(slot, OwnSlot::Global(_)))
            .chain(
                self.static_keys
                    .iter()
                    .filter(move |(owner, _)| {
                        matches!(slot, OwnSlot::Static(family, _) if *owner == family)
                    })
                    .map(|(_, key)| key),
            )
            .chain(
                self.prototype_keys
                    .iter()
                    .filter(move |(owner, _)| {
                        matches!(slot, OwnSlot::PrototypeProperty(family, _) if *owner == family)
                    })
                    .map(|(_, key)| key),
            )
            .filter(move |key| may_name(key));
        let any = self
            .any_receiver_keys
            .iter()
            .filter(move |key| may_name(key));
        (exact, any)
    }

    /// Whether a recorded key may name `slot`, counting writes on exact
    /// receivers only with `exact_receivers`.
    fn reached_by_key(&self, slot: OwnSlot<'_>, exact_receivers: bool) -> bool {
        if let OwnSlot::Global(family) = slot
            && family.global_binding().is_none()
        {
            return true;
        }
        if let OwnSlot::Species(family) = slot {
            return (exact_receivers && self.species_exact(family)) || self.any_receiver_symbol;
        }
        let (mut exact, mut any) = self.keys_naming(slot);
        (exact_receivers && exact.next().is_some()) || any.next().is_some()
    }

    /// Whether the slot is unknown through a write whose receiver may be any
    /// native object, or an unknown key on the global object.
    fn reached_through_global(&self, slot: OwnSlot<'_>) -> bool {
        self.every_slot
            || self.reached_by_key(slot, false)
            || matches!(slot, OwnSlot::Global(_))
                && self.global_keys.contains(&NativePropertyKey::Unknown)
    }

    /// A Symbol- or unknown-keyed write on the exact constructor.
    fn species_exact(&self, family: NativeIntrinsic) -> bool {
        self.symbol_statics.contains(&family)
            || self
                .static_keys
                .contains(&(family, NativePropertyKey::Unknown))
    }

    /// How this one witness's writes reach `slot`, if they do.
    fn reach(&self, slot: OwnSlot<'_>) -> Option<SlotReach> {
        if self.every_slot {
            return Some(SlotReach::UnknownReceiver);
        }
        if let OwnSlot::Species(family) = slot {
            return if self.symbol_statics.contains(&family) {
                Some(SlotReach::NamedWrite)
            } else if self.species_exact(family) {
                Some(SlotReach::UnknownKey)
            } else {
                self.any_receiver_symbol
                    .then_some(SlotReach::UnknownReceiver)
            };
        }
        let (exact, mut any) = self.keys_naming(slot);
        let exact: Vec<_> = exact.collect();
        if exact
            .iter()
            .any(|key| matches!(key, NativePropertyKey::String(_)))
        {
            Some(SlotReach::NamedWrite)
        } else if !exact.is_empty() {
            Some(SlotReach::UnknownKey)
        } else {
            any.next().map(|_| SlotReach::UnknownReceiver)
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

    /// An own-property slot is intact when no census write reaches it. A
    /// whole prototype slot covers every property on the object, so it stays
    /// family-level: intact only when neither its family nor the global
    /// object has a known or possible write.
    fn census_intact(&self, slot: IntrinsicSlot) -> bool {
        self.census_closed
            && match slot {
                IntrinsicSlot::Global(_)
                | IntrinsicSlot::Static(..)
                | IntrinsicSlot::PrototypeProperty(..)
                | IntrinsicSlot::Species(_) => self.own_slot_writes.intact(slot),
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
        slots
            .iter()
            .flat_map(|slot| {
                slot_mutated_roots(&self.mutated, &self.own_slot_writes, &SlotName::from(*slot))
            })
            .collect()
    }
}

/// The families (and the global object) that make one slot unknown.
fn slot_mutated_roots(
    mutated: &BTreeSet<NativeIntrinsic>,
    own_slot_writes: &OwnSlotWrites,
    slot: &SlotName,
) -> BTreeSet<NativeIntrinsic> {
    match slot.own() {
        Some(own) if own_slot_writes.own_intact(own) => BTreeSet::new(),
        Some(own) => BTreeSet::from([if own_slot_writes.reached_through_global(own) {
            NativeIntrinsic::GlobalObject
        } else {
            own.family()
        }]),
        None => [slot.family(), NativeIntrinsic::GlobalObject]
            .into_iter()
            .filter(|root| mutated.contains(root))
            .collect(),
    }
}

/// Why one census witness makes a slot unknown (`debundle explain-integrity`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SlotReach {
    /// The write's receiver and string key name the slot, or it assigns the
    /// slot's global binding.
    NamedWrite,
    /// A prototype slot is family-level: any write to its family makes it
    /// unknown, whatever the receiver and key.
    FamilyCoupling,
    /// A write to the global object, which makes every prototype slot unknown.
    GlobalObjectCoupling,
    /// An exact receiver with a key that may name the slot: unknown, affixed,
    /// or derived through slots the census cannot keep intact.
    UnknownKey,
    /// The receiver may be any native object, or a global-object alias was
    /// rebound.
    UnknownReceiver,
}

/// A completed whole-program census, as `program_graph.json` records it,
/// queried slot by slot. It mirrors [`IntegrityFlags`] without author axioms.
pub struct CensusExplanation<'a> {
    witnesses: &'a [NativeMutationWitness],
    dynamic_scope: bool,
    mutated: BTreeSet<NativeIntrinsic>,
    own_slot_writes: OwnSlotWrites,
}

/// One slot's census verdict and every witness reaching it, by index.
pub struct SlotExplanation {
    pub intact: bool,
    pub affected_roots: BTreeSet<NativeIntrinsic>,
    pub reaches: Vec<(usize, SlotReach)>,
}

impl<'a> CensusExplanation<'a> {
    /// `dynamic_scope`: a direct `eval` or `with` leaves the census open and
    /// the global object mutated without a witness.
    pub fn new(witnesses: &'a [NativeMutationWitness], dynamic_scope: bool) -> Self {
        let mut mutated: BTreeSet<_> = witnesses
            .iter()
            .filter(|witness| witness.unreachable_branch.is_none())
            .flat_map(|witness| witness.affected_roots.iter().copied())
            .collect();
        if dynamic_scope {
            mutated.insert(NativeIntrinsic::GlobalObject);
        }
        let own_slot_writes = OwnSlotWrites::resolve(witnesses, &mutated, |slot| {
            prototype_slot_intact(&mutated, slot)
        });
        Self {
            witnesses,
            dynamic_scope,
            mutated,
            own_slot_writes,
        }
    }

    pub fn explain(&self, slot: &SlotName) -> SlotExplanation {
        let affected_roots = slot_mutated_roots(&self.mutated, &self.own_slot_writes, slot);
        let reaches = self
            .witnesses
            .iter()
            .enumerate()
            .filter(|(_, witness)| {
                witness.unreachable_branch.is_none() && !witness.affected_roots.is_empty()
            })
            .filter_map(|(index, witness)| Some((index, self.reach(witness, slot)?)))
            .collect();
        SlotExplanation {
            intact: !self.dynamic_scope && affected_roots.is_empty(),
            affected_roots,
            reaches,
        }
    }

    fn reach(&self, witness: &NativeMutationWitness, slot: &SlotName) -> Option<SlotReach> {
        if let Some(own) = slot.own() {
            let mut single = OwnSlotWrites::default();
            single.add(witness, &self.own_slot_writes, |slot| {
                prototype_slot_intact(&self.mutated, slot)
            });
            return single.reach(own);
        }
        let family = slot.family();
        if !witness.affected_roots.contains(&family)
            && !witness
                .affected_roots
                .contains(&NativeIntrinsic::GlobalObject)
        {
            return None;
        }
        let receiver = slot.prototype_receiver();
        Some(
            if witness
                .target_slots
                .iter()
                .any(|target| Some(&target.receiver) == receiver.as_ref())
            {
                SlotReach::NamedWrite
            } else if witness.affected_roots.contains(&family) {
                SlotReach::FamilyCoupling
            } else if !witness.target_slots.is_empty()
                && witness
                    .target_slots
                    .iter()
                    .all(|target| target.receiver != NativeReceiverPath::Unknown)
            {
                SlotReach::GlobalObjectCoupling
            } else {
                SlotReach::UnknownReceiver
            },
        )
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
            IntrinsicSlot::PrototypeProperty(NativeIntrinsic::Array, "map"),
            IntrinsicSlot::Species(NativeIntrinsic::Array),
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
            receiver_global: None,
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
    fn an_affixed_key_reaches_only_slots_it_may_name() {
        let affixed = |prefix: &str, suffix: &str| {
            witness(
                NativeIntrinsic::GlobalObject,
                NativeReceiverPath::Unknown,
                NativePropertyKey::Affixed {
                    prefix: prefix.to_owned(),
                    suffix: suffix.to_owned(),
                },
                &[],
            )
        };
        assert_eq!(
            intact(&[affixed("migration.", "")], PROBES),
            [MAP, OBJECT, KEYS, ENTRIES, SYMBOL_FOR]
        );
        assert_eq!(
            intact(&[affixed("", "ys")], PROBES),
            [MAP, OBJECT, ENTRIES, SYMBOL_FOR]
        );
        // `"Ma" + x + "p"` may be `"Map"`.
        assert_eq!(
            intact(&[affixed("Ma", "p")], PROBES),
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
            intact(std::slice::from_ref(&entries), PROBES),
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
            intact(std::slice::from_ref(&symbol_keyed), PROBES),
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
        // A binding declared in the chunk has no global target slot.
        rebind.target_slots.clear();
        assert_eq!(
            intact(&[rebind], PROBES),
            [OBJECT, KEYS, ENTRIES, SYMBOL_FOR, OBJECT_PROTOTYPE]
        );
    }

    /// A prototype property is reached by a write naming it on that exact
    /// prototype or on any receiver, never by writes to the global object or
    /// to a constructor; `@@species` only by Symbol- or unknown-keyed writes
    /// that may land on the constructor.
    #[test]
    fn prototype_properties_and_species_are_reached_by_key() {
        use NativeIntrinsic::{Array, GlobalObject};
        const MAP: IntrinsicSlot = IntrinsicSlot::PrototypeProperty(Array, "map");
        const FOR_EACH: IntrinsicSlot = IntrinsicSlot::PrototypeProperty(Array, "forEach");
        const SPECIES: IntrinsicSlot = IntrinsicSlot::Species(Array);
        const PROBES: &[IntrinsicSlot] = &[MAP, FOR_EACH, SPECIES];
        let prototype = NativeReceiverPath::Prototype(Array);
        let constructor = NativeReceiverPath::Constructor(Array);
        for (witnesses, expected) in [
            (
                vec![witness(Array, prototype.clone(), string("map"), &[])],
                vec![FOR_EACH, SPECIES],
            ),
            (
                vec![witness(
                    Array,
                    prototype.clone(),
                    NativePropertyKey::Unknown,
                    &[],
                )],
                vec![SPECIES],
            ),
            (
                vec![witness(Array, constructor.clone(), string("from"), &[])],
                vec![MAP, FOR_EACH, SPECIES],
            ),
            (
                vec![witness(
                    Array,
                    constructor.clone(),
                    NativePropertyKey::OtherSymbol,
                    &[],
                )],
                vec![MAP, FOR_EACH],
            ),
            (
                vec![witness(Array, constructor, NativePropertyKey::Unknown, &[])],
                vec![MAP, FOR_EACH],
            ),
            (
                vec![witness(
                    GlobalObject,
                    NativeReceiverPath::GlobalObject,
                    string("map"),
                    &[],
                )],
                vec![MAP, FOR_EACH, SPECIES],
            ),
            (
                vec![witness(
                    GlobalObject,
                    NativeReceiverPath::Unknown,
                    string("map"),
                    &[],
                )],
                vec![FOR_EACH, SPECIES],
            ),
            (
                vec![witness(
                    GlobalObject,
                    NativeReceiverPath::Unknown,
                    NativePropertyKey::OtherSymbol,
                    &[],
                )],
                vec![MAP, FOR_EACH],
            ),
            (
                vec![witness(
                    GlobalObject,
                    NativeReceiverPath::Unknown,
                    NativePropertyKey::Unknown,
                    &[],
                )],
                vec![],
            ),
        ] {
            assert_eq!(intact(&witnesses, PROBES), expected, "{witnesses:?}");
        }
        // A replaced `Array` binding makes `Array.prototype.x` any-receiver.
        let mut rebind = witness(
            Array,
            NativeReceiverPath::GlobalObject,
            string("Array"),
            &[],
        );
        rebind.operation = NativeMutationOperation::BindingAssignment;
        assert_eq!(
            intact(
                &[
                    rebind,
                    witness(
                        Array,
                        NativeReceiverPath::Prototype(Array),
                        string("other"),
                        &[]
                    )
                ],
                PROBES
            ),
            PROBES
        );
    }

    #[test]
    fn slot_names_parse_their_display_and_member_forms() {
        for (text, slot) in [
            (
                "%Array.prototype%",
                SlotName::Prototype(NativeIntrinsic::Array),
            ),
            (
                "Set.prototype.add",
                SlotName::PrototypeProperty(NativeIntrinsic::Set, "add".to_owned()),
            ),
            (
                "%Array[@@species]%",
                SlotName::Species(NativeIntrinsic::Array),
            ),
            (
                "Object.defineProperty",
                SlotName::Static(NativeIntrinsic::Object, "defineProperty".to_owned()),
            ),
            ("Set", SlotName::Global(NativeIntrinsic::Set)),
            ("%ArrayIteratorPrototype%", SlotName::ArrayIteratorPrototype),
        ] {
            assert_eq!(text.parse::<SlotName>(), Ok(slot.clone()), "{text}");
            assert_eq!(slot.to_string().parse::<SlotName>(), Ok(slot));
        }
        for text in ["globalThis", "Foo.bar", "", "Array.from[@@species]"] {
            assert!(text.parse::<SlotName>().is_err(), "{text}");
        }
    }

    #[test]
    fn each_witness_reaches_a_slot_by_how_its_receiver_and_key_name_it() {
        let census = [
            witness(
                NativeIntrinsic::Object,
                NativeReceiverPath::Constructor(NativeIntrinsic::Object),
                string("entries"),
                &[],
            ),
            witness(
                NativeIntrinsic::Object,
                NativeReceiverPath::Constructor(NativeIntrinsic::Object),
                NativePropertyKey::Unknown,
                &[],
            ),
            witness(
                NativeIntrinsic::GlobalObject,
                NativeReceiverPath::Unknown,
                string("keys"),
                &[],
            ),
            witness(
                NativeIntrinsic::GlobalObject,
                NativeReceiverPath::GlobalObject,
                string("flag"),
                &[],
            ),
            witness(
                NativeIntrinsic::Object,
                NativeReceiverPath::Prototype(NativeIntrinsic::Object),
                NativePropertyKey::Unknown,
                &[],
            ),
        ];
        let explanation = CensusExplanation::new(&census, false);
        let reaches = |slot: SlotName| explanation.explain(&slot).reaches;
        use SlotReach::*;
        assert_eq!(
            reaches(SlotName::Static(
                NativeIntrinsic::Object,
                "entries".to_owned()
            )),
            [(0, NamedWrite), (1, UnknownKey)]
        );
        assert_eq!(
            reaches(SlotName::Static(NativeIntrinsic::Object, "keys".to_owned())),
            [(1, UnknownKey), (2, UnknownReceiver)]
        );
        assert_eq!(
            reaches(SlotName::Prototype(NativeIntrinsic::Object)),
            [
                (0, FamilyCoupling),
                (1, FamilyCoupling),
                (2, UnknownReceiver),
                (3, GlobalObjectCoupling),
                (4, NamedWrite)
            ]
        );
        assert_eq!(
            reaches(SlotName::Prototype(NativeIntrinsic::Array)),
            [(2, UnknownReceiver), (3, GlobalObjectCoupling)]
        );
        let set = SlotName::Global(NativeIntrinsic::Set);
        assert!(explanation.explain(&set).intact);
        assert!(reaches(set.clone()).is_empty());
        // Dynamic scope leaves the census open without a witness.
        assert!(!CensusExplanation::new(&census, true).explain(&set).intact);
    }
}
