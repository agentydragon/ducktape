//! Property absence on the intrinsic Object prototype, separate from native call identity.

use super::*;
use crate::reports::schema::{NativePrototypeMutation, NativePrototypeMutationKind};
use swc_common::DUMMY_SP;

fn descriptor_method(property: &str) -> bool {
    matches!(
        property,
        "defineProperty"
            | "defineProperties"
            | "setPrototypeOf"
            | "__defineGetter__"
            | "__defineSetter__"
    )
}

fn contained_roots(
    value: &ValueExpr,
    aliases: &BTreeMap<Id, IntrinsicRoots>,
    index: &ExecutionIndex,
) -> IntrinsicRoots {
    let mut roots = intrinsic_roots(value, aliases, &index.global_object_names);
    match value {
        ValueExpr::ObjectLiteral(_, properties) => {
            for (_, value) in properties {
                roots.extend(contained_roots(value, aliases, index));
            }
        }
        ValueExpr::Origin(ValueOrigin::Function(key)) => {
            for id in index
                .prototype_callable_reads
                .get(key)
                .into_iter()
                .flatten()
            {
                roots.extend(intrinsic_roots(
                    &ValueExpr::Binding(id.clone()),
                    aliases,
                    &index.global_object_names,
                ));
            }
        }
        ValueExpr::Origin(ValueOrigin::Class(key)) => {
            if let Some(owner) = index.class_owners.get(key) {
                for (key, callable) in &index.callables {
                    if callable.owner == *owner {
                        for id in index
                            .prototype_callable_reads
                            .get(key)
                            .into_iter()
                            .flatten()
                        {
                            roots.extend(intrinsic_roots(
                                &ValueExpr::Binding(id.clone()),
                                aliases,
                                &index.global_object_names,
                            ));
                        }
                    }
                }
            }
            if let Some(class) = index.class_values.get(key) {
                for value in class.static_fields.values() {
                    roots.extend(contained_roots(value, aliases, index));
                }
            }
        }
        ValueExpr::Member(base, ..) => roots.extend(contained_roots(base, aliases, index)),
        ValueExpr::Call(callee, args, _) | ValueExpr::New(callee, args, _) => {
            roots.extend(contained_roots(callee, aliases, index));
            for argument in args {
                roots.extend(contained_roots(argument, aliases, index));
            }
        }
        ValueExpr::Alternatives(options) => {
            for option in options {
                roots.extend(contained_roots(option, aliases, index));
            }
        }
        _ => {}
    }
    // Reflect does not have a family in the native-call catalogue, but is a
    // descriptor capability when it is passed or destructured.
    if matches!(value, ValueExpr::Binding(id) if id.0.as_ref() == "Reflect") {
        roots.insert(IntrinsicRoot::Object);
    }
    roots
}

impl ExecutionIndex {
    pub(super) fn collect_prototype_mutations(
        &self,
        aliases: &BTreeMap<Id, IntrinsicRoots>,
        assignments: &[&(Id, ValueExpr)],
        declared: &BTreeSet<Id>,
    ) -> Vec<NativePrototypeMutation> {
        // Capability containment is separate from receiver identity: a record
        // holding a native is not itself a native mutation receiver.
        let mut capabilities = aliases.clone();
        loop {
            let mut changed = false;
            for (id, value) in
                assignments
                    .iter()
                    .copied()
                    .map(|(id, value)| (id.clone(), value.clone()))
                    .chain(self.hoisted_bindings.iter().map(|(id, key)| {
                        (id.clone(), ValueExpr::Origin(ValueOrigin::Function(*key)))
                    }))
                    .chain(
                        self.class_bindings.iter().map(|(key, id)| {
                            (id.clone(), ValueExpr::Origin(ValueOrigin::Class(*key)))
                        }),
                    )
            {
                let roots = contained_roots(&value, &capabilities, self);
                let entry = capabilities.entry(id).or_default();
                let before = entry.len();
                entry.extend(roots);
                changed |= entry.len() != before;
            }
            if !changed {
                break;
            }
        }
        let mutation = |owner, span, kind, key: Option<&PropertyKeyExpr>| {
            let evidence =
                key.map(|key| property_key_evidence(key, &self.property_key_assignments, declared));
            let properties = match evidence.as_ref().map(|evidence| &evidence.domain) {
                Some(PropertyKeyDomain::FiniteStrings(keys))
                    if !keys.contains("__proto__") && !keys.contains("prototype") =>
                {
                    Some(keys.clone())
                }
                Some(PropertyKeyDomain::Symbol) => Some(BTreeSet::new()),
                _ => None,
            };
            NativePrototypeMutation {
                chunk_id: String::new(),
                statement_ordinal: owner,
                source_location: None,
                kind,
                properties,
                key_dependencies: evidence
                    .map(|evidence| evidence.dependencies)
                    .unwrap_or_default(),
                span,
            }
        };
        let mut mutations = Vec::new();
        for write in &self.intrinsic_writes {
            // Every syntactic property writer is relevant until its receiver
            // has an allocation proof. This includes borrowed methods and
            // native aliases stored in arbitrary source-owned containers.
            if write.constructor_this.is_some_and(|key| {
                self.local_constructors
                    .values()
                    .any(|constructor| constructor.key == Some(key))
            }) {
                continue;
            }
            if self.dynamic_scope_owners.is_empty()
                && self
                    .receiver_identity
                    .receiver(write.receiver_callable, &write.receiver, write.span)
                    .is_some()
            {
                continue;
            }
            let kind = match write.operation {
                NativeMutationOperation::PropertyAssignment | NativeMutationOperation::Update => {
                    NativePrototypeMutationKind::Write
                }
                NativeMutationOperation::Delete => NativePrototypeMutationKind::Delete,
                _ => NativePrototypeMutationKind::Reflective,
            };
            // A reflective operation can also affect descriptors or prototypes;
            // initially retain its whole property surface as unknown.
            let key = (kind != NativePrototypeMutationKind::Reflective)
                .then_some(write.key.as_ref())
                .flatten();
            mutations.push(mutation(write.owner, write.span, kind, key));
        }
        // A native descriptor capability can escape through an alias, bind/call,
        // an imported helper, or a legacy accessor method. Such paths are not
        // closed by the older native-family mutation census.
        for call in &self.prototype_calls {
            let member = match &call.callee {
                ValueExpr::Member(_, property, _) => Some(property),
                _ => None,
            };
            if member.is_some_and(|property| property.as_deref().is_none_or(descriptor_method)) {
                mutations.push(mutation(
                    call.owner,
                    call.span,
                    NativePrototypeMutationKind::Reflective,
                    None,
                ));
                continue;
            }
            if call.args.iter().any(|argument| {
                let roots = contained_roots(argument, &capabilities, self);
                !roots.is_empty()
            }) {
                mutations.push(mutation(
                    call.owner,
                    call.span,
                    NativePrototypeMutationKind::Escape,
                    None,
                ));
            }
        }
        for owner in &self.dynamic_scope_owners {
            mutations.push(mutation(
                *owner,
                DUMMY_SP,
                NativePrototypeMutationKind::DynamicScope,
                None,
            ));
        }
        mutations
    }
}
