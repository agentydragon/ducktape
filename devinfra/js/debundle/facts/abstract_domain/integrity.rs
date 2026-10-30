//! Integrity flags for intrinsic slots.
//!
//! A11 supplies every slot's original identity. The closed-write census can
//! keep a slot `Intact`, or an exact-site `IntrinsicIntact` axiom can assert it
//! at one source site; otherwise the slot is unknown and its consumer falls
//! back to its opaque behavior. Rules and builtin rows name slots; nothing but
//! this module reads the census for them.

use crate::analysis_inputs::{NativeIntrinsic, NativeOperationInputRef, ProofDependencies};
use std::collections::{BTreeMap, BTreeSet};

/// One intrinsic slot. Each belongs to a census family; a write anywhere in
/// that family or to the global object makes it unknown.
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

/// The completed census as slot flags, plus the axioms resolved from validated
/// author inputs. The default answers `Unknown` for every census query.
#[derive(Debug, Clone, Default)]
pub(crate) struct IntegrityFlags {
    /// Every analyzed chunk contributed its writes and no dynamic scope can
    /// hide one.
    census_closed: bool,
    mutated: BTreeSet<NativeIntrinsic>,
    census_dependencies: ProofDependencies,
    axioms: BTreeMap<u32, IntrinsicIntact>,
    prototype_writes: PrototypeWrites,
}

impl IntegrityFlags {
    pub(crate) fn new(
        census_closed: bool,
        mutated: BTreeSet<NativeIntrinsic>,
        census_dependencies: ProofDependencies,
        axioms: BTreeMap<u32, IntrinsicIntact>,
        prototype_writes: PrototypeWrites,
    ) -> Self {
        Self {
            census_closed,
            mutated,
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

    /// Slot-level is family-level: a slot is intact only when neither its
    /// family nor the global object has a known or possible write. A write
    /// through an unresolved global-slot value is recorded as a global-object
    /// write, so it poisons every slot.
    fn census_intact(&self, slot: IntrinsicSlot) -> bool {
        self.census_closed
            && !self.mutated.contains(&NativeIntrinsic::GlobalObject)
            && !self.mutated.contains(&slot.family())
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
            .map(|slot| slot.family())
            .chain([NativeIntrinsic::GlobalObject])
            .filter(|root| self.mutated.contains(root))
            .collect()
    }
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

    /// Exhaustive over families: a slot is intact exactly when the census is
    /// closed and neither its family nor the global object is mutated.
    #[test]
    fn slots_inherit_family_and_global_writes() {
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
}
