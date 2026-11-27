//! Native intrinsic roots reachable from values, and the alias closure over them.

use super::receiver_flow::CallableReturnInputs;
use super::{BundleGlobals, GlobalNames};
use analysis_domain::builtins;
use analysis_domain::integrity::IntrinsicSlot;
use analysis_vocabulary::inputs::{NativeIntrinsic, ValueInputRef};
use analysis_vocabulary::value::Kind;
use inference::{FactContext, IterativeInference, IterativeRules};
use source_identity::SourceUnitOrdinal;
use std::collections::{BTreeMap, BTreeSet};
use swc_ecma_ast::*;
use value_model::{MemberKey, ValueExpr};

pub(super) type IntrinsicRoots = BTreeSet<NativeIntrinsic>;

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
pub(super) enum NativeReceiverRoot {
    Intrinsic(NativeIntrinsic),
    IntrinsicPrototype(NativeIntrinsic),
    IntrinsicDescendant(NativeIntrinsic),
    ArrayIteratorPrototype,
    SetIteratorPrototype,
    OrdinaryDirect {
        reference: ValueInputRef,
        available_after: SourceUnitOrdinal,
    },
    OrdinaryDescendant {
        reference: ValueInputRef,
        available_after: SourceUnitOrdinal,
    },
    /// The value of a bundle-owned global property: never an intrinsic
    /// (`GlobalNames::bundle`). Its descendants stay unknown.
    BundleGlobal,
}

impl NativeReceiverRoot {
    pub(super) fn cloned_ordinary_input(&self) -> Option<(ValueInputRef, SourceUnitOrdinal)> {
        match self {
            Self::OrdinaryDirect {
                reference,
                available_after,
            }
            | Self::OrdinaryDescendant {
                reference,
                available_after,
            } => Some((reference.clone(), *available_after)),
            Self::Intrinsic(_)
            | Self::IntrinsicPrototype(_)
            | Self::IntrinsicDescendant(_)
            | Self::ArrayIteratorPrototype
            | Self::SetIteratorPrototype
            | Self::BundleGlobal => None,
        }
    }

    pub(super) fn intrinsic_root(&self) -> Option<NativeIntrinsic> {
        match self {
            Self::Intrinsic(root)
            | Self::IntrinsicPrototype(root)
            | Self::IntrinsicDescendant(root) => Some(*root),
            Self::ArrayIteratorPrototype
            | Self::SetIteratorPrototype
            | Self::OrdinaryDirect { .. }
            | Self::OrdinaryDescendant { .. }
            | Self::BundleGlobal => None,
        }
    }
}

pub(super) type NativeReceiverRoots = BTreeSet<NativeReceiverRoot>;

/// A return that is statically primitive cannot expose a captured native
/// descendant. This is a value-shape boundary; it does not assert that
/// evaluating the callable is pure or safe.
/// The named native binding a write on a proved global object can replace.
/// Reading an unrelated global slot instead yields an unknown value identity.
/// Any mutation through an unresolved global-slot value can reach a native
/// object. Preserve that uncertainty instead of treating it as the global
/// object itself and discarding an unrelated descendant property name.
pub(super) fn native_mutation_roots(mut roots: IntrinsicRoots) -> IntrinsicRoots {
    if roots.remove(&NativeIntrinsic::UnknownGlobalValue) {
        roots.insert(NativeIntrinsic::GlobalObject);
    }
    roots
}

pub(super) struct ReceiverRootProjection {
    pub(super) prior_roots: IntrinsicRoots,
    pub(super) effective_roots: IntrinsicRoots,
    pub(super) affected_roots: IntrinsicRoots,
    pub(super) value_input_refs: BTreeSet<ValueInputRef>,
    /// A bundle-owned global value was discharged.
    pub(super) bundle_global: bool,
}

pub(super) fn project_receiver_roots(
    roots: &NativeReceiverRoots,
    owner: SourceUnitOrdinal,
) -> ReceiverRootProjection {
    let mut prior_roots = IntrinsicRoots::new();
    let mut effective_roots = IntrinsicRoots::new();
    let mut value_input_refs = BTreeSet::new();
    let mut bundle_global = false;
    for root in roots {
        match root {
            NativeReceiverRoot::Intrinsic(root)
            | NativeReceiverRoot::IntrinsicPrototype(root)
            | NativeReceiverRoot::IntrinsicDescendant(root) => {
                prior_roots.insert(*root);
                effective_roots.insert(*root);
            }
            NativeReceiverRoot::ArrayIteratorPrototype
            | NativeReceiverRoot::SetIteratorPrototype => {
                prior_roots.insert(NativeIntrinsic::UnknownGlobalValue);
                effective_roots.insert(NativeIntrinsic::UnknownGlobalValue);
            }
            NativeReceiverRoot::OrdinaryDirect {
                reference,
                available_after,
            } if owner > *available_after => {
                // The input discharges this prior unknown-global possibility
                // only for the exact captured receiver, preserving the fact
                // dependency even when the resulting mutation roots are empty.
                prior_roots.insert(NativeIntrinsic::UnknownGlobalValue);
                value_input_refs.insert(reference.clone());
            }
            NativeReceiverRoot::OrdinaryDirect { .. } => {
                // The initializer or stable copy has not completed at this
                // observation. Preserve the prior opaque behavior.
                prior_roots.insert(NativeIntrinsic::UnknownGlobalValue);
                effective_roots.insert(NativeIntrinsic::UnknownGlobalValue);
            }
            NativeReceiverRoot::OrdinaryDescendant {
                reference,
                available_after,
            } => {
                prior_roots.insert(NativeIntrinsic::UnknownGlobalValue);
                effective_roots.insert(NativeIntrinsic::UnknownGlobalValue);
                if owner > *available_after {
                    value_input_refs.insert(reference.clone());
                }
            }
            // A bundle-owned global's value is discharged by the host claim.
            NativeReceiverRoot::BundleGlobal => {
                prior_roots.insert(NativeIntrinsic::UnknownGlobalValue);
                bundle_global = true;
            }
        }
    }
    let affected_roots = native_mutation_roots(effective_roots.clone());
    ReceiverRootProjection {
        prior_roots,
        effective_roots,
        affected_roots,
        value_input_refs,
        bundle_global,
    }
}

/// The own-data property of the unshadowed global `Object` an exact member
/// read names, as the `%Object.<property>%` slot spells it.
pub fn object_own_data_read_property(
    value: &ValueExpr,
    is_declared: impl Fn(&Id) -> bool,
) -> Option<&'static str> {
    let [
        IntrinsicSlot::Global(NativeIntrinsic::Object),
        IntrinsicSlot::Static(NativeIntrinsic::Object, property),
    ] = native_own_data_read_slots(value, is_declared)?
    else {
        return None;
    };
    builtins::data_property("Object", property)
        .filter(|data| data.value == builtins::PropertyValue::Function)
        .map(|data| data.name)
}

/// Slot obligations for an exact native own-data lookup. Receiver and key
/// evaluation remain with the indexed operation's caller.
pub fn native_own_data_read_slots(
    value: &ValueExpr,
    is_declared: impl Fn(&Id) -> bool,
) -> Option<[IntrinsicSlot; 2]> {
    let ValueExpr::Member(receiver, Some(property), MemberKey::Static, _) = value else {
        return None;
    };
    let ValueExpr::Binding(binding) = receiver.as_ref() else {
        return None;
    };
    if is_declared(binding) {
        return None;
    }
    let family = NativeIntrinsic::for_global_binding(binding.0.as_ref())?;
    let property = builtins::own_data_property_name(binding.0.as_ref(), property)?;
    Some([
        IntrinsicSlot::Global(family),
        IntrinsicSlot::Static(family, property),
    ])
}

pub(super) fn receiver_uses_intrinsic_alias(
    value: &ValueExpr,
    aliases: &BTreeMap<Id, NativeReceiverRoots>,
) -> bool {
    match value {
        ValueExpr::Binding(id) => aliases.contains_key(id),
        ValueExpr::Member(base, ..) => receiver_uses_intrinsic_alias(base, aliases),
        ValueExpr::Alternatives(_) | ValueExpr::Conditional { .. } | ValueExpr::Sequence(_) => {
            value
                .branching_alternatives()
                .iter()
                .any(|option| receiver_uses_intrinsic_alias(option, aliases))
        }
        _ => false,
    }
}

/// The global-object property whose value the receiver is read from, by
/// spelling: `location` for `window.location.href = url` or
/// `location.href = url`.
pub(super) fn receiver_global_property(
    value: &ValueExpr,
    aliases: &BTreeMap<Id, NativeReceiverRoots>,
    globals: &GlobalNames,
    declared: &BTreeSet<Id>,
) -> Option<String> {
    let global_object = |id: &Id| {
        (!declared.contains(id) && globals.contains(id.0.as_ref()))
            || aliases.get(id).is_some_and(|roots| {
                roots.contains(&NativeReceiverRoot::Intrinsic(
                    NativeIntrinsic::GlobalObject,
                ))
            })
    };
    match value {
        ValueExpr::Binding(id)
            if !declared.contains(id)
                && !global_object(id)
                && globals.binding_root(id.0.as_ref()).is_none() =>
        {
            Some(id.0.to_string())
        }
        ValueExpr::Member(base, property, _, _) => match base.as_ref() {
            ValueExpr::Binding(id) if global_object(id) => property.clone(),
            base => receiver_global_property(base, aliases, globals, declared),
        },
        _ => None,
    }
}

/// Access to the mutable inputs of the shared root transfer. The ordinary
/// census uses closed maps; bundle ownership installs watched invalidation reads.
pub(super) trait IntrinsicRootRead {
    fn binding_roots(&mut self, binding: &Id) -> NativeReceiverRoots;
    fn bundle_holds(&mut self, property: Option<&str>, key: &MemberKey) -> bool;
    fn fresh_collections(&mut self) -> bool;
}

pub(super) struct IntrinsicRootMap<'a> {
    pub(super) aliases: &'a BTreeMap<Id, NativeReceiverRoots>,
    pub(super) bundle: &'a BundleGlobals,
}

impl IntrinsicRootRead for IntrinsicRootMap<'_> {
    fn binding_roots(&mut self, binding: &Id) -> NativeReceiverRoots {
        self.aliases.get(binding).cloned().unwrap_or_default()
    }

    fn bundle_holds(&mut self, property: Option<&str>, key: &MemberKey) -> bool {
        self.bundle.holds(property, key)
    }
    fn fresh_collections(&mut self) -> bool {
        self.bundle.fresh_collections
    }
}

pub(super) fn intrinsic_roots(
    value: &ValueExpr,
    roots: &BTreeMap<Id, NativeReceiverRoots>,
    globals: &GlobalNames,
    declared: &BTreeSet<Id>,
    ordinary_inputs: &BTreeMap<Id, (ValueInputRef, SourceUnitOrdinal)>,
    callable_return_inputs: &CallableReturnInputs,
) -> NativeReceiverRoots {
    intrinsic_roots_read(
        value,
        &mut IntrinsicRootMap {
            aliases: roots,
            bundle: &globals.bundle,
        },
        globals,
        declared,
        ordinary_inputs,
        callable_return_inputs,
    )
}

pub(super) fn intrinsic_alternative_can_be_unknown(value: &ValueExpr) -> bool {
    !value.cannot_denote_an_object()
}

pub(super) fn intrinsic_roots_read(
    value: &ValueExpr,
    inputs: &mut impl IntrinsicRootRead,
    globals: &GlobalNames,
    declared: &BTreeSet<Id>,
    ordinary_inputs: &BTreeMap<Id, (ValueInputRef, SourceUnitOrdinal)>,
    callable_return_inputs: &CallableReturnInputs,
) -> NativeReceiverRoots {
    let intrinsic = |root| NativeReceiverRoot::Intrinsic(root);
    match value {
        ValueExpr::Binding(id) if ordinary_inputs.contains_key(id) => {
            let (reference, available_after) = &ordinary_inputs[id];
            BTreeSet::from([NativeReceiverRoot::OrdinaryDirect {
                reference: reference.clone(),
                available_after: *available_after,
            }])
        }
        ValueExpr::ArrayIteratorPrototype(expression) => {
            let mut bindings = BTreeSet::new();
            expression.bindings(&mut bindings);
            if bindings.iter().any(|binding| {
                declared.contains(binding) && matches!(binding.0.as_ref(), "Object" | "Symbol")
            }) {
                BTreeSet::from([intrinsic(NativeIntrinsic::UnknownGlobalValue)])
            } else {
                BTreeSet::from([NativeReceiverRoot::ArrayIteratorPrototype])
            }
        }
        ValueExpr::SetIteratorPrototype(_) => {
            BTreeSet::from([NativeReceiverRoot::SetIteratorPrototype])
        }
        // The spelling of a local binding is not an intrinsic identity.
        // Retain source-only aliases, but never manufacture a native root for
        // a shadowed constructor or global-object name.
        ValueExpr::Binding(id) if declared.contains(id) => inputs.binding_roots(id),
        ValueExpr::Binding(id) if id.0.as_ref() == "Error" => {
            BTreeSet::from([intrinsic(NativeIntrinsic::Error)])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "Function" => {
            BTreeSet::from([intrinsic(NativeIntrinsic::Function)])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "Object" => {
            BTreeSet::from([intrinsic(NativeIntrinsic::Object)])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "Array" => {
            BTreeSet::from([intrinsic(NativeIntrinsic::Array)])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "Math" => {
            BTreeSet::from([intrinsic(NativeIntrinsic::Math)])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "RegExp" => {
            BTreeSet::from([intrinsic(NativeIntrinsic::RegExp)])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "Symbol" => {
            BTreeSet::from([intrinsic(NativeIntrinsic::Symbol)])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "Promise" => {
            BTreeSet::from([intrinsic(NativeIntrinsic::Promise)])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "Map" => {
            BTreeSet::from([intrinsic(NativeIntrinsic::Map)])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "Set" => {
            BTreeSet::from([intrinsic(NativeIntrinsic::Set)])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "WeakMap" => {
            BTreeSet::from([intrinsic(NativeIntrinsic::WeakMap)])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "WeakSet" => {
            BTreeSet::from([intrinsic(NativeIntrinsic::WeakSet)])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "FinalizationRegistry" => {
            BTreeSet::from([intrinsic(NativeIntrinsic::FinalizationRegistry)])
        }
        ValueExpr::Binding(id) if globals.contains(id.0.as_ref()) => {
            BTreeSet::from([intrinsic(NativeIntrinsic::GlobalObject)])
        }
        ValueExpr::Binding(id) => inputs.binding_roots(id),
        ValueExpr::Alloc(Kind::Function, _) => NativeReceiverRoots::new(),
        ValueExpr::ObjectLiteral(_, fields) => {
            // A fresh container is not native merely because it stores a
            // native value. Preserve this value-input extension only when a
            // field actually carries the selected ordinary input.
            let mut references = BTreeSet::new();
            for (_, field) in fields.iter() {
                for root in intrinsic_roots_read(
                    field,
                    inputs,
                    globals,
                    declared,
                    ordinary_inputs,
                    callable_return_inputs,
                ) {
                    match root {
                        NativeReceiverRoot::OrdinaryDirect {
                            reference,
                            available_after,
                        }
                        | NativeReceiverRoot::OrdinaryDescendant {
                            reference,
                            available_after,
                        } => {
                            references.insert((reference, available_after));
                        }
                        NativeReceiverRoot::Intrinsic(_)
                        | NativeReceiverRoot::IntrinsicPrototype(_)
                        | NativeReceiverRoot::IntrinsicDescendant(_)
                        | NativeReceiverRoot::ArrayIteratorPrototype
                        | NativeReceiverRoot::SetIteratorPrototype
                        | NativeReceiverRoot::BundleGlobal => {}
                    }
                }
            }
            let mut out = NativeReceiverRoots::new();
            if !references.is_empty() {
                out.insert(intrinsic(NativeIntrinsic::UnknownGlobalValue));
                out.extend(references.into_iter().map(|(reference, available_after)| {
                    NativeReceiverRoot::OrdinaryDescendant {
                        reference,
                        available_after,
                    }
                }));
            }
            out
        }
        ValueExpr::Alternatives(_) | ValueExpr::Conditional { .. } | ValueExpr::Sequence(_) => {
            let options = value.branching_alternatives();
            let mut combined = NativeReceiverRoots::new();
            let mut unknown_arm = false;
            let mut ordinary_refs = BTreeSet::new();
            for option in options {
                let option_roots = intrinsic_roots_read(
                    option,
                    inputs,
                    globals,
                    declared,
                    ordinary_inputs,
                    callable_return_inputs,
                );
                ordinary_refs.extend(
                    option_roots
                        .iter()
                        .filter_map(NativeReceiverRoot::cloned_ordinary_input),
                );
                if option_roots.is_empty() && intrinsic_alternative_can_be_unknown(option) {
                    unknown_arm = true;
                }
                combined.extend(option_roots);
            }
            if unknown_arm {
                for (reference, available_after) in ordinary_refs {
                    combined.insert(intrinsic(NativeIntrinsic::UnknownGlobalValue));
                    combined.insert(NativeReceiverRoot::OrdinaryDescendant {
                        reference,
                        available_after,
                    });
                }
            }
            combined
        }
        ValueExpr::Member(base, property, key, _) => {
            let base_roots = intrinsic_roots_read(
                base,
                inputs,
                globals,
                declared,
                ordinary_inputs,
                callable_return_inputs,
            );
            intrinsic_member_roots(base_roots, property.as_deref(), key, inputs, globals)
        }
        ValueExpr::Call(callee, args, _) | ValueExpr::New(callee, args, _) => {
            let mut input_roots = NativeReceiverRoots::new();
            let mut ordinary_refs = BTreeSet::new();
            let mut object_get_prototype_of = false;
            for (position, value) in std::iter::once(callee.as_ref())
                .chain(args.iter())
                .enumerate()
            {
                // The member receiver and its reflection guard share this one
                // child result; nested reflected callees do not revisit it.
                let value_roots = if position == 0
                    && let ValueExpr::Member(receiver, Some(property), MemberKey::Static, _) = value
                    && property == "getPrototypeOf"
                {
                    let receiver_roots = intrinsic_roots_read(
                        receiver,
                        inputs,
                        globals,
                        declared,
                        ordinary_inputs,
                        callable_return_inputs,
                    );
                    object_get_prototype_of = receiver_roots
                        .contains(&NativeReceiverRoot::Intrinsic(NativeIntrinsic::Object));
                    intrinsic_member_roots(
                        receiver_roots,
                        Some(property.as_str()),
                        &MemberKey::Static,
                        inputs,
                        globals,
                    )
                } else {
                    intrinsic_roots_read(
                        value,
                        inputs,
                        globals,
                        declared,
                        ordinary_inputs,
                        callable_return_inputs,
                    )
                };
                ordinary_refs.extend(
                    value_roots
                        .iter()
                        .filter_map(NativeReceiverRoot::cloned_ordinary_input),
                );
                input_roots.extend(value_roots);
            }
            if matches!(value, ValueExpr::Call(..)) && object_get_prototype_of {
                // A prototype query can cross intrinsic families or reach a
                // builtin through an ordinary input. Keep its result opaque
                // and carry the possible native roots through result aliases;
                // a later write must not evade the completed census.
                input_roots.insert(intrinsic(NativeIntrinsic::UnknownGlobalValue));
                return input_roots;
            }
            let return_roots = match callee.as_ref() {
                ValueExpr::Binding(binding) => callable_return_inputs.by_binding.get(binding),
                ValueExpr::Alloc(Kind::Function, function) => {
                    callable_return_inputs.by_origin.get(function)
                }
                _ => None,
            };
            if let Some(roots) = return_roots {
                ordinary_refs.extend(
                    roots
                        .iter()
                        .filter_map(NativeReceiverRoot::cloned_ordinary_input),
                );
                input_roots.extend(roots.iter().cloned());
            }
            // Preserve baseline opaque call/construct behavior unless an
            // author input participates. A known empty return summary
            // still preserves the collected input roots.
            if ordinary_refs.is_empty() {
                return if return_roots.is_some() {
                    input_roots
                } else {
                    NativeReceiverRoots::new()
                };
            }
            let mut out = input_roots;
            out.insert(intrinsic(NativeIntrinsic::UnknownGlobalValue));
            out.extend(
                ordinary_refs
                    .into_iter()
                    .map(
                        |(reference, available_after)| NativeReceiverRoot::OrdinaryDescendant {
                            reference,
                            available_after,
                        },
                    ),
            );
            out
        }
        _ => NativeReceiverRoots::new(),
    }
}

pub(super) fn intrinsic_member_roots(
    base_roots: NativeReceiverRoots,
    property: Option<&str>,
    key: &MemberKey,
    inputs: &mut impl IntrinsicRootRead,
    globals: &GlobalNames,
) -> NativeReceiverRoots {
    let intrinsic = NativeReceiverRoot::Intrinsic;
    let mut out = NativeReceiverRoots::new();
    for root in base_roots {
        match root {
            NativeReceiverRoot::OrdinaryDirect {
                reference,
                available_after,
            }
            | NativeReceiverRoot::OrdinaryDescendant {
                reference,
                available_after,
            } => {
                out.insert(intrinsic(NativeIntrinsic::UnknownGlobalValue));
                out.insert(NativeReceiverRoot::OrdinaryDescendant {
                    reference,
                    available_after,
                });
            }
            NativeReceiverRoot::Intrinsic(NativeIntrinsic::GlobalObject) => {
                if inputs.bundle_holds(property, key) {
                    out.insert(NativeReceiverRoot::BundleGlobal);
                } else {
                    out.insert(intrinsic(
                        property
                            .and_then(|property| globals.binding_root(property))
                            .unwrap_or(NativeIntrinsic::UnknownGlobalValue),
                    ));
                }
            }
            NativeReceiverRoot::BundleGlobal => {
                out.insert(intrinsic(NativeIntrinsic::UnknownGlobalValue));
            }
            NativeReceiverRoot::Intrinsic(NativeIntrinsic::UnknownGlobalValue) => {
                out.insert(intrinsic(NativeIntrinsic::UnknownGlobalValue));
            }
            NativeReceiverRoot::Intrinsic(root) => {
                out.insert(if property == Some("prototype") {
                    NativeReceiverRoot::IntrinsicPrototype(root)
                } else {
                    NativeReceiverRoot::IntrinsicDescendant(root)
                });
                // These paths can cross from one intrinsic family to
                // another; computed keys can name either one. Preserve
                // the known family and add an opaque root so writes
                // through aliases conservatively invalidate all native
                // proofs that may depend on the resolved object.
                if property.is_none_or(|property| matches!(property, "__proto__" | "constructor")) {
                    out.insert(intrinsic(NativeIntrinsic::UnknownGlobalValue));
                }
            }
            NativeReceiverRoot::IntrinsicPrototype(root)
            | NativeReceiverRoot::IntrinsicDescendant(root) => {
                out.insert(NativeReceiverRoot::IntrinsicDescendant(root));
                if property.is_none_or(|property| matches!(property, "__proto__" | "constructor")) {
                    out.insert(intrinsic(NativeIntrinsic::UnknownGlobalValue));
                }
            }
            NativeReceiverRoot::ArrayIteratorPrototype
            | NativeReceiverRoot::SetIteratorPrototype => {
                out.insert(intrinsic(NativeIntrinsic::UnknownGlobalValue));
            }
        }
    }
    out
}

pub(super) fn read_intrinsic_alias_inputs(
    context: &mut impl FactContext<Id, NativeReceiverRoots>,
    value: &ValueExpr,
) -> BTreeMap<Id, NativeReceiverRoots> {
    let mut mentioned = BTreeSet::new();
    value.bindings(&mut mentioned);
    read_intrinsic_aliases(context, mentioned)
}

pub(super) fn read_intrinsic_aliases(
    context: &mut impl FactContext<Id, NativeReceiverRoots>,
    bindings: impl IntoIterator<Item = Id>,
) -> BTreeMap<Id, NativeReceiverRoots> {
    bindings
        .into_iter()
        .filter_map(|binding| {
            context
                .read(binding.clone())
                .cloned()
                .map(|roots| (binding, roots))
        })
        .collect()
}

pub(super) struct RawIntrinsicAliasRules<'a> {
    pub(super) assignments: &'a [&'a (Id, ValueExpr)],
    pub(super) globals: &'a GlobalNames,
    pub(super) declared: &'a BTreeSet<Id>,
}

impl IterativeRules for RawIntrinsicAliasRules<'_> {
    type Key = Id;
    type Fact = NativeReceiverRoots;
    type Rule = usize;

    type MergeScratch = ();
    fn evaluate(&self, rule: &Self::Rule, context: &mut impl FactContext<Self::Key, Self::Fact>) {
        let (target, value) = self.assignments[*rule];
        let aliases = read_intrinsic_alias_inputs(context, value);
        let roots = intrinsic_roots(
            value,
            &aliases,
            self.globals,
            self.declared,
            &BTreeMap::new(),
            &CallableReturnInputs::default(),
        );
        if !roots.is_empty() {
            context.propose(target.clone(), roots);
        }
    }

    fn merge(_: &Self::Key, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
        let previous_len = current.len();
        current.extend(contribution);
        current.len() != previous_len
    }
}

pub(super) fn close_raw_intrinsic_aliases(
    assignments: &[&(Id, ValueExpr)],
    globals: &GlobalNames,
    declared: &BTreeSet<Id>,
) -> BTreeMap<Id, NativeReceiverRoots> {
    IterativeInference::new(RawIntrinsicAliasRules {
        assignments,
        globals,
        declared,
    })
    .solve(0..assignments.len())
    .into_values()
}

#[cfg(test)]
mod iterative_alias_closure_tests {
    use super::super::deferred_global_publication::close_prepublication_derived_bindings;
    use super::super::infer_mutation_receiver_aliases;
    use super::{
        GlobalNames, MemberKey, NativeReceiverRoot, ValueExpr, close_raw_intrinsic_aliases,
    };
    use analysis_vocabulary::inputs::NativeIntrinsic;
    use std::collections::{BTreeMap, BTreeSet};
    use std::sync::Arc;
    use swc_common::SyntaxContext;
    use swc_ecma_ast::Id;
    use value_model::NamespaceValueFacts;

    fn id(name: &str) -> Id {
        (name.into(), SyntaxContext::empty())
    }

    #[test]
    fn receiver_alias_watchers_propagate_backward_assignment_chains() {
        let namespace = id("namespace");
        let first = id("first");
        let second = id("second");
        let assignments = [
            (first.clone(), ValueExpr::Binding(second.clone())),
            (second.clone(), ValueExpr::Binding(namespace.clone())),
        ];
        let assignment_refs = assignments.iter().collect::<Vec<_>>();

        let aliases = infer_mutation_receiver_aliases(
            &namespace,
            &assignment_refs,
            &BTreeSet::new(),
            &NamespaceValueFacts::default(),
        );

        assert_eq!(aliases, BTreeSet::from([first, second]));
    }

    #[test]
    fn receiver_alias_watchers_include_projected_data_members() {
        let namespace = id("namespace");
        let holder = id("holder");
        let candidate = id("candidate");
        let projected = id("projected");
        let assignments = [
            (
                projected.clone(),
                ValueExpr::Member(
                    Arc::new(ValueExpr::Binding(holder.clone())),
                    Some("member".to_owned()),
                    MemberKey::Static,
                    swc_common::DUMMY_SP.into(),
                ),
            ),
            (
                holder.clone(),
                ValueExpr::ObjectLiteral(
                    7,
                    [
                        (
                            Some("owner".to_owned()),
                            ValueExpr::Binding(namespace.clone()),
                        ),
                        (
                            Some("member".to_owned()),
                            ValueExpr::Binding(candidate.clone()),
                        ),
                    ]
                    .into(),
                ),
            ),
            (candidate.clone(), ValueExpr::Unknown),
        ];
        let assignment_refs = assignments.iter().collect::<Vec<_>>();
        let facts = NamespaceValueFacts::from_assignments(
            BTreeMap::new(),
            BTreeSet::from([7]),
            &assignment_refs,
        );

        let aliases =
            infer_mutation_receiver_aliases(&namespace, &assignment_refs, &BTreeSet::new(), &facts);

        assert_eq!(aliases, BTreeSet::from([candidate, holder, projected]));
    }

    #[test]
    fn raw_intrinsic_aliases_remain_source_only_and_follow_late_roots() {
        let array = id("Array");
        let first = id("first");
        let second = id("second");
        let assignments = [
            (first.clone(), ValueExpr::Binding(second.clone())),
            (second.clone(), ValueExpr::Binding(array.clone())),
        ];
        let assignment_refs = assignments.iter().collect::<Vec<_>>();

        let aliases = close_raw_intrinsic_aliases(
            &assignment_refs,
            &GlobalNames::default(),
            &BTreeSet::new(),
        );

        assert_eq!(
            aliases.get(&first),
            Some(&BTreeSet::from([NativeReceiverRoot::Intrinsic(
                NativeIntrinsic::Array
            )]))
        );
        assert_eq!(
            aliases.get(&second),
            Some(&BTreeSet::from([NativeReceiverRoot::Intrinsic(
                NativeIntrinsic::Array
            )]))
        );
        assert!(!aliases.contains_key(&array));
    }

    #[test]
    fn raw_intrinsic_aliases_do_not_turn_shadowed_names_into_native_roots() {
        let local_set = id("Set");
        let alias = id("alias");
        let assignments = [(alias.clone(), ValueExpr::Binding(local_set.clone()))];
        let assignment_refs = assignments.iter().collect::<Vec<_>>();

        let aliases = close_raw_intrinsic_aliases(
            &assignment_refs,
            &GlobalNames::default(),
            &BTreeSet::from([local_set]),
        );
        assert!(!aliases.contains_key(&alias));
    }

    #[test]
    fn prepublication_derived_bindings_wake_backward_assignment_chains() {
        let alias = id("alias");
        let holder = id("holder");
        let projected = id("projected");
        let unrelated = id("unrelated");
        let assignments = [
            (
                projected.clone(),
                ValueExpr::Member(
                    Arc::new(ValueExpr::Binding(holder.clone())),
                    Some("member".to_owned()),
                    MemberKey::Static,
                    swc_common::DUMMY_SP.into(),
                ),
            ),
            (
                holder.clone(),
                ValueExpr::ObjectLiteral(
                    9,
                    [
                        (Some("member".to_owned()), ValueExpr::Binding(alias.clone())),
                        (
                            Some("other".to_owned()),
                            ValueExpr::Binding(unrelated.clone()),
                        ),
                    ]
                    .into(),
                ),
            ),
        ];

        assert_eq!(
            close_prepublication_derived_bindings(&alias, &assignments),
            BTreeSet::from([alias, holder, projected])
        );
    }
}
