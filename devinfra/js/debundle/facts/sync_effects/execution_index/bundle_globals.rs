//! Bundle-owned global properties: one finite invalidation closure across chunks.

use super::super::value_model::{MemberKey, ValueExpr};
use super::{
    CallKind, CallableReturnInputs, ExecutionIndex, GlobalNames, IntrinsicRootMap,
    IntrinsicRootRead, IntrinsicWriteTarget, NativeReceiverRoot, NativeReceiverRoots,
    PreparedCallableCapabilities, PropertyKeyDomain, PropertyKeyEvidence, PropertyKeyExpr,
    close_raw_intrinsic_aliases, intrinsic_roots_read, key_domain_may_name, property_key_evidence,
};
use analysis::facts::standard_globals::is_predefined_global;
use analysis_vocabulary::inputs::{
    BundleGlobalNames, NativeIntrinsic, NoAccessorInstallRef, ResolvedNoAccessorInstall,
};
use analysis_vocabulary::report_schema::NativeMutationOperation;
use inference::{FactContext, IterativeInference, IterativeRules};
use std::collections::{BTreeMap, BTreeSet};
use swc_ecma_ast::Id;

const KEYED_COLLECTIONS: [&str; 4] = ["Map", "Set", "WeakMap", "WeakSet"];
const LEGACY_ACCESSOR_METHODS: [&str; 2] = ["__defineGetter__", "__defineSetter__"];

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
enum Key {
    Name(usize),
    RegisteredSymbols,
    FreshCollections,
    /// Preserve the existing chunk-local key-domain guard.
    SymbolFor(usize),
    Alias(usize, Id),
    Rejected(usize),
    All,
}

trait Invalidations {
    fn contains(&mut self, key: Key) -> bool;

    fn global(&mut self, key: Key) -> bool {
        self.contains(Key::All) || self.contains(key)
    }
}

struct Watched<'a, C>(&'a mut C);

impl<C: FactContext<Key, ()>> Invalidations for Watched<'_, C> {
    fn contains(&mut self, key: Key) -> bool {
        self.0.read(key).is_some()
    }
}

struct Closed<'a>(&'a BTreeMap<Key, ()>);

impl Invalidations for Closed<'_> {
    fn contains(&mut self, key: Key) -> bool {
        self.0.contains_key(&key)
    }
}

/// The only slot-specific exception is a sloppy read of the slot itself.
/// Factoring it out lets every possible key of a write share one value transfer.
#[derive(Clone, Copy)]
enum Ownership<'a> {
    Any,
    SameName(&'a str),
    None,
}

impl<'a> Ownership<'a> {
    fn meet(self, other: Self) -> Self {
        match (self, other) {
            (Self::Any, value) | (value, Self::Any) => value,
            (Self::SameName(left), Self::SameName(right)) if left == right => self,
            _ => Self::None,
        }
    }

    fn holds(self, name: Option<&str>) -> bool {
        match self {
            Self::Any => true,
            Self::SameName(expected) => name == Some(expected),
            Self::None => false,
        }
    }
}

struct BundleGlobalContext<'a, Inputs> {
    inputs: &'a mut Inputs,
    globals: &'a GlobalNames,
    declared: &'a BTreeSet<Id>,
    fresh_objects: &'a BTreeSet<Id>,
}

impl<Inputs: IntrinsicRootRead> BundleGlobalContext<'_, Inputs> {
    fn roots(&mut self, value: &ValueExpr) -> NativeReceiverRoots {
        intrinsic_roots_read(
            value,
            self.inputs,
            self.globals,
            self.declared,
            &BTreeMap::new(),
            &CallableReturnInputs::default(),
        )
    }

    fn ownership<'a>(&mut self, value: &'a ValueExpr) -> Ownership<'a> {
        match value {
            ValueExpr::ObjectLiteral(..)
            | ValueExpr::Primitive(_)
            | ValueExpr::StrictEquality(..)
            | ValueExpr::LogicalNot(_)
            | ValueExpr::Void(_)
            | ValueExpr::Undefined
            | ValueExpr::NonCallable(_) => Ownership::Any,
            ValueExpr::Alternatives(_) | ValueExpr::Conditional { .. } | ValueExpr::Sequence(_) => {
                value
                    .result_alternatives()
                    .expect("alternative expression")
                    .iter()
                    .fold(Ownership::Any, |current, option| {
                        current.meet(self.ownership(option))
                    })
            }
            ValueExpr::New(callee, _, _)
                if matches!(callee.as_ref(), ValueExpr::Binding(id)
                    if !self.declared.contains(id) && KEYED_COLLECTIONS.contains(&id.0.as_ref()))
                    && self.inputs.fresh_collections() =>
            {
                Ownership::Any
            }
            ValueExpr::Binding(id) if self.fresh_objects.contains(id) => Ownership::Any,
            _ => {
                let roots = self.roots(value);
                if !roots.is_empty()
                    && roots
                        .iter()
                        .all(|root| *root == NativeReceiverRoot::BundleGlobal)
                {
                    Ownership::Any
                } else if let ValueExpr::Binding(id) = value
                    && !self.declared.contains(id)
                {
                    Ownership::SameName(id.0.as_ref())
                } else {
                    Ownership::None
                }
            }
        }
    }
}

struct PreparedKey {
    evidence: PropertyKeyEvidence,
    names: Vec<usize>,
    registered_symbols: bool,
    symbol_for: bool,
    fresh_collections: bool,
    rejects: bool,
}

impl PreparedKey {
    fn new(evidence: PropertyKeyEvidence) -> Self {
        let domain = &evidence.domain;
        Self {
            names: Vec::new(),
            registered_symbols: matches!(
                domain,
                PropertyKeyDomain::Symbol | PropertyKeyDomain::Unknown
            ),
            symbol_for: key_domain_may_name(domain, "for") || key_domain_may_name(domain, "Symbol"),
            fresh_collections: KEYED_COLLECTIONS
                .iter()
                .any(|name| key_domain_may_name(domain, name)),
            rejects: matches!(domain, PropertyKeyDomain::Unknown)
                || key_domain_may_name(domain, "__proto__"),
            evidence,
        }
    }

    fn exact(&self, module: usize, invalid: &mut impl Invalidations) -> bool {
        self.evidence.dependencies.is_empty()
            || self.evidence.dependencies == BTreeSet::from([NativeIntrinsic::Symbol])
                && !invalid.contains(Key::SymbolFor(module))
    }
}

struct PreparedWrite<'a> {
    source: &'a IntrinsicWriteTarget,
    keys: Vec<PreparedKey>,
}

struct PreparedAccessor<'a> {
    base: &'a ValueExpr,
    key: PreparedKey,
    premise: Option<&'a NoAccessorInstallRef>,
    may_install: bool,
}

struct Module<'a> {
    index: &'a ExecutionIndex,
    globals: GlobalNames,
    declared: BTreeSet<Id>,
    assignments: Vec<&'a (Id, ValueExpr)>,
    aliases: BTreeMap<Id, NativeReceiverRoots>,
    fresh_objects: BTreeSet<Id>,
    writes: Vec<PreparedWrite<'a>>,
    accessors: Vec<PreparedAccessor<'a>>,
}

impl<'a> Module<'a> {
    fn prepare(index: &'a ExecutionIndex, premises: &'a [ResolvedNoAccessorInstall]) -> Self {
        let declared = index.declared_bindings();
        let assignments = index.bundle_assignments();
        let aliases = close_raw_intrinsic_aliases(&assignments, &index.census_globals, &declared);
        let evidence = |key: &PropertyKeyExpr| {
            property_key_evidence(key, &index.property_key_assignments, &declared)
        };
        let writes = index
            .intrinsic_writes
            .iter()
            .map(|source| PreparedWrite {
                source,
                keys: source
                    .key
                    .iter()
                    .chain(&source.additional_keys)
                    .map(|key| PreparedKey::new(evidence(key)))
                    .collect(),
            })
            .collect();
        let accessors = index
            .prototype_calls
            .iter()
            .filter_map(|call| {
                let ValueExpr::Member(base, property, key, _) = &call.callee else {
                    return None;
                };
                if call.kind != CallKind::Call {
                    return None;
                }
                let evidence = match (property, key) {
                    (Some(name), _) => PropertyKeyEvidence {
                        domain: PropertyKeyDomain::FiniteStrings(BTreeSet::from([name.clone()])),
                        dependencies: BTreeSet::new(),
                    },
                    (None, MemberKey::Computed(Some(binding))) => {
                        evidence(&PropertyKeyExpr::Binding(binding.clone()))
                    }
                    _ => PropertyKeyEvidence::unknown(),
                };
                let may_install = LEGACY_ACCESSOR_METHODS
                    .iter()
                    .any(|method| key_domain_may_name(&evidence.domain, method));
                Some(PreparedAccessor {
                    base,
                    key: PreparedKey::new(evidence),
                    premise: premises
                        .iter()
                        .find(|premise| {
                            property.is_none() && premise.function_span.contains(call.span)
                        })
                        .map(|premise| &premise.reference),
                    may_install,
                })
            })
            .collect();
        let mut globals = index.census_globals.clone();
        globals.bundle.symbol_keys = index.symbol_key_bindings(&declared);
        Self {
            index,
            globals,
            declared,
            assignments,
            aliases,
            fresh_objects: index.fresh_object_bindings(),
            writes,
            accessors,
        }
    }

    fn candidates(&self) -> BundleGlobalNames {
        let mut inputs = IntrinsicRootMap {
            aliases: &self.aliases,
            bundle: &self.globals.bundle,
        };
        let mut context = BundleGlobalContext {
            inputs: &mut inputs,
            globals: &self.globals,
            declared: &self.declared,
            fresh_objects: &self.fresh_objects,
        };
        let mut candidates = BundleGlobalNames {
            fresh_collections: true,
            ..BundleGlobalNames::default()
        };
        for write in &self.writes {
            if write.source.value.is_none()
                || !context
                    .roots(&write.source.receiver)
                    .contains(&NativeReceiverRoot::Intrinsic(
                        NativeIntrinsic::GlobalObject,
                    ))
            {
                continue;
            }
            if write.source.key.is_none() {
                continue;
            }
            let key = &write.keys[0];
            match &key.evidence.domain {
                PropertyKeyDomain::FiniteStrings(keys) => candidates.names.extend(
                    keys.iter()
                        .filter(|name| {
                            !is_predefined_global(name, self.globals.predefined)
                                && self.globals.binding_root(name).is_none()
                        })
                        .cloned(),
                ),
                PropertyKeyDomain::Symbol => candidates.registered_symbols = true,
                _ => {}
            }
        }
        for expression in self
            .index
            .intrinsic_writes
            .iter()
            .flat_map(|write| std::iter::once(&write.receiver).chain(&write.value))
            .chain(self.index.prototype_calls.iter().map(|call| &call.callee))
            .chain(self.assignments.iter().map(|(_, value)| value))
        {
            global_property_reads(
                &self.globals,
                expression,
                &mut context,
                &mut candidates.names,
            );
        }
        candidates
    }

    fn initialize(&mut self, candidates: &BundleGlobalNames, names: &BTreeMap<&str, usize>) {
        let mut optimistic = self.globals.clone();
        optimistic.bundle.names = candidates.names.clone();
        optimistic.bundle.registered_symbols = candidates.registered_symbols;
        optimistic.bundle.fresh_collections = candidates.fresh_collections;
        self.aliases = close_raw_intrinsic_aliases(&self.assignments, &optimistic, &self.declared);
        for write in &mut self.writes {
            for key in &mut write.keys {
                key.names = match &key.evidence.domain {
                    PropertyKeyDomain::FiniteStrings(keys) => keys
                        .iter()
                        .filter_map(|name| names.get(name.as_str()).copied())
                        .collect(),
                    domain => names
                        .iter()
                        .filter(|(name, _)| key_domain_may_name(domain, name))
                        .map(|(_, &key)| key)
                        .collect(),
                };
            }
        }
    }
}

struct RootRead<'a, 'b, I> {
    module: usize,
    source: &'a Module<'a>,
    names: &'a BTreeMap<&'a str, usize>,
    registered_symbols: bool,
    invalid: &'b mut I,
}

impl<I: Invalidations> IntrinsicRootRead for RootRead<'_, '_, I> {
    fn binding_roots(&mut self, binding: &Id) -> NativeReceiverRoots {
        let mut roots = self
            .source
            .aliases
            .get(binding)
            .cloned()
            .unwrap_or_default();
        // Concrete native identities and emptiness are fixed by preparation;
        // only an opaque alternative can grow (docs/inference.md).
        if roots.contains(&NativeReceiverRoot::BundleGlobal)
            && !roots.contains(&NativeReceiverRoot::Intrinsic(
                NativeIntrinsic::UnknownGlobalValue,
            ))
            && self
                .invalid
                .contains(Key::Alias(self.module, binding.clone()))
        {
            roots.insert(NativeReceiverRoot::Intrinsic(
                NativeIntrinsic::UnknownGlobalValue,
            ));
        }
        roots
    }

    fn bundle_holds(&mut self, property: Option<&str>, key: &MemberKey) -> bool {
        match (property, key) {
            (Some(name), _) => self
                .names
                .get(name)
                .is_some_and(|&name| !self.invalid.global(Key::Name(name))),
            (None, MemberKey::Computed(Some(binding))) => {
                self.registered_symbols
                    && self.source.globals.bundle.symbol_keys.contains(binding)
                    && !self.invalid.global(Key::RegisteredSymbols)
            }
            _ => false,
        }
    }

    fn fresh_collections(&mut self) -> bool {
        !self.invalid.global(Key::FreshCollections)
    }
}

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
enum Rule {
    Alias(usize, usize),
    Write(usize, usize),
    Assignment(usize, usize),
    Accessor(usize, usize),
}

struct Rules<'a> {
    modules: Vec<Module<'a>>,
    names: BTreeMap<&'a str, usize>,
    spellings: Vec<&'a str>,
    registered_symbols: bool,
}

impl<'a> Rules<'a> {
    fn new(mut modules: Vec<Module<'a>>, candidates: &'a BundleGlobalNames) -> Self {
        let spellings: Vec<_> = candidates.names.iter().map(String::as_str).collect();
        let names = spellings
            .iter()
            .copied()
            .enumerate()
            .map(|(index, name)| (name, index))
            .collect();
        for source in &mut modules {
            source.initialize(candidates, &names);
        }
        Self {
            modules,
            names,
            spellings,
            registered_symbols: candidates.registered_symbols,
        }
    }

    fn producers(&self) -> Vec<Rule> {
        let mut rules = Vec::new();
        for (module, source) in self.modules.iter().enumerate() {
            for (index, (binding, _)) in source.assignments.iter().enumerate() {
                if source.aliases.get(binding).is_some_and(|roots| {
                    roots.contains(&NativeReceiverRoot::BundleGlobal)
                        && !roots.contains(&NativeReceiverRoot::Intrinsic(
                            NativeIntrinsic::UnknownGlobalValue,
                        ))
                }) {
                    rules.push(Rule::Alias(module, index));
                }
                if !source.declared.contains(binding) {
                    rules.push(Rule::Assignment(module, index));
                }
            }
            rules.extend((0..source.writes.len()).map(|index| Rule::Write(module, index)));
            rules.extend((0..source.accessors.len()).map(|index| Rule::Accessor(module, index)));
        }
        rules
    }

    fn transfer(&self, rule: Rule, invalid: &mut impl Invalidations) -> Vec<Key> {
        let (module, index) = match rule {
            Rule::Alias(module, index)
            | Rule::Write(module, index)
            | Rule::Assignment(module, index)
            | Rule::Accessor(module, index) => (module, index),
        };
        let source = &self.modules[module];
        let mut inputs = RootRead {
            module,
            source,
            names: &self.names,
            registered_symbols: self.registered_symbols,
            invalid,
        };
        let mut context = BundleGlobalContext {
            inputs: &mut inputs,
            globals: &source.globals,
            declared: &source.declared,
            fresh_objects: &source.fresh_objects,
        };
        let reject = || vec![Key::All, Key::Rejected(module)];
        let mut out = Vec::new();
        match rule {
            Rule::Alias(..) => {
                let (binding, value) = source.assignments[index];
                if context
                    .roots(value)
                    .contains(&NativeReceiverRoot::Intrinsic(
                        NativeIntrinsic::UnknownGlobalValue,
                    ))
                {
                    out.push(Key::Alias(module, binding.clone()));
                }
            }
            Rule::Write(..) => {
                let write = &source.writes[index];
                let roots = context.roots(&write.source.receiver);
                if roots
                    .iter()
                    .all(|root| *root == NativeReceiverRoot::BundleGlobal)
                {
                    return out;
                }
                if write.source.key.is_none() {
                    return reject();
                }
                let key = &write.keys[0];
                if write.source.operation == NativeMutationOperation::Delete {
                    if roots.contains(&NativeReceiverRoot::Intrinsic(
                        NativeIntrinsic::GlobalObject,
                    )) {
                        out.extend(key.names.iter().copied().map(Key::Name));
                        if key.registered_symbols {
                            out.push(Key::RegisteredSymbols);
                        }
                    }
                    return out;
                }
                let ownership =
                    if write.source.operation == NativeMutationOperation::PropertyAssignment {
                        write
                            .source
                            .value
                            .as_ref()
                            .map(|value| context.ownership(value))
                            .unwrap_or(Ownership::None)
                    } else {
                        Ownership::None
                    };
                for key in &write.keys {
                    if !key.exact(module, context.inputs.invalid) || key.rejects {
                        return reject();
                    }
                    if key.symbol_for {
                        out.extend([Key::SymbolFor(module), Key::RegisteredSymbols]);
                    }
                    if key.fresh_collections {
                        out.push(Key::FreshCollections);
                    }
                    for &name in &key.names {
                        let spelling = self.spellings[name];
                        if !ownership.holds(Some(spelling)) {
                            out.push(Key::Name(name));
                        }
                    }
                    if matches!(key.evidence.domain, PropertyKeyDomain::Symbol)
                        && !ownership.holds(None)
                    {
                        out.push(Key::RegisteredSymbols);
                    }
                }
            }
            Rule::Assignment(..) => {
                let (binding, value) = source.assignments[index];
                if let Some(&name) = self.names.get(binding.0.as_ref())
                    && !context.ownership(value).holds(Some(binding.0.as_ref()))
                {
                    out.push(Key::Name(name));
                }
                if binding.0.as_ref() == "Symbol" {
                    out.extend([Key::SymbolFor(module), Key::RegisteredSymbols]);
                }
                if KEYED_COLLECTIONS.contains(&binding.0.as_ref()) {
                    out.push(Key::FreshCollections);
                }
            }
            Rule::Accessor(..) => {
                if self.accessor_needed(module, &source.accessors[index], &mut context)
                    && source.accessors[index].premise.is_none()
                {
                    return reject();
                }
            }
        }
        out
    }

    fn accessor_needed<I: Invalidations>(
        &self,
        module: usize,
        call: &PreparedAccessor<'_>,
        context: &mut BundleGlobalContext<'_, RootRead<'_, '_, I>>,
    ) -> bool {
        (call.may_install || !call.key.exact(module, context.inputs.invalid))
            && !context
                .roots(call.base)
                .iter()
                .all(|root| *root == NativeReceiverRoot::BundleGlobal)
    }

    fn project(&self, invalid: &BTreeMap<Key, ()>) -> BundleGlobalNames {
        let mut closed = Closed(invalid);
        let mut result = BundleGlobalNames {
            names: self
                .names
                .iter()
                .filter(|(_, index)| !closed.global(Key::Name(**index)))
                .map(|(name, _)| (*name).to_owned())
                .collect(),
            registered_symbols: self.registered_symbols && !closed.global(Key::RegisteredSymbols),
            fresh_collections: !closed.global(Key::FreshCollections),
            no_accessor_install_callables: BTreeSet::new(),
        };
        for (module, source) in self.modules.iter().enumerate() {
            if closed.contains(Key::Rejected(module)) {
                continue;
            }
            let mut inputs = RootRead {
                module,
                source,
                names: &self.names,
                registered_symbols: self.registered_symbols,
                invalid: &mut closed,
            };
            let mut context = BundleGlobalContext {
                inputs: &mut inputs,
                globals: &source.globals,
                declared: &source.declared,
                fresh_objects: &source.fresh_objects,
            };
            for call in &source.accessors {
                if self.accessor_needed(module, call, &mut context) {
                    result
                        .no_accessor_install_callables
                        .insert(call.premise.expect("unrejected accessor premise").clone());
                }
            }
        }
        result
    }
}

impl IterativeRules for &Rules<'_> {
    type Key = Key;
    type Fact = ();
    type Rule = Rule;

    type MergeScratch = ();
    fn evaluate(&self, rule: &Rule, context: &mut impl FactContext<Key, ()>) {
        for key in self.transfer(*rule, &mut Watched(context)) {
            context.propose(key, ());
        }
    }

    fn merge(_: &Key, _: &mut (), _: ()) -> bool {
        false
    }
}

pub fn resolve<'a>(
    modules: impl IntoIterator<
        Item = (
            &'a PreparedCallableCapabilities,
            &'a [ResolvedNoAccessorInstall],
        ),
    >,
) -> BundleGlobalNames {
    let modules: Vec<_> = modules
        .into_iter()
        .map(|(prepared, premises)| Module::prepare(&prepared.index, premises))
        .collect();
    if modules
        .iter()
        .any(|source| source.globals.predefined == spec::PredefinedGlobals::Unknown)
    {
        return BundleGlobalNames::default();
    }
    let candidates = candidate_union(&modules);
    let rules = Rules::new(modules, &candidates);
    let producers = rules.producers();
    let inference = IterativeInference::new(&rules);
    let invalid = inference.solve_values(producers);
    rules.project(&invalid)
}

fn candidate_union(modules: &[Module<'_>]) -> BundleGlobalNames {
    let mut candidates = BundleGlobalNames {
        fresh_collections: true,
        ..BundleGlobalNames::default()
    };
    for source in modules {
        let local = source.candidates();
        candidates.names.extend(local.names);
        candidates.registered_symbols |= local.registered_symbols;
    }
    candidates
}

/// Non-standard property names `expr` reads from the global object.
fn global_property_reads(
    globals: &GlobalNames,
    expr: &ValueExpr,
    context: &mut BundleGlobalContext<'_, impl IntrinsicRootRead>,
    reads: &mut BTreeSet<String>,
) {
    match expr {
        ValueExpr::Member(base, property, _, _) => {
            if let Some(name) = property
                && !reads.contains(name)
                && !is_predefined_global(name, globals.predefined)
                && globals.binding_root(name).is_none()
                && context.roots(base).contains(&NativeReceiverRoot::Intrinsic(
                    NativeIntrinsic::GlobalObject,
                ))
            {
                reads.insert(name.clone());
            }
            global_property_reads(globals, base, context, reads);
        }
        ValueExpr::Call(callee, arguments, _) | ValueExpr::New(callee, arguments, _) => {
            global_property_reads(globals, callee, context, reads);
            for argument in arguments.iter() {
                global_property_reads(globals, argument, context, reads);
            }
        }
        ValueExpr::ObjectLiteral(_, properties) => {
            for (_, value) in properties.iter() {
                global_property_reads(globals, value, context, reads);
            }
        }
        ValueExpr::ArrayIteratorPrototype(inner) | ValueExpr::SetIteratorPrototype(inner) => {
            global_property_reads(globals, inner, context, reads);
        }
        ValueExpr::Primitive(values)
        | ValueExpr::Alternatives(values)
        | ValueExpr::Sequence(values) => {
            for value in values.iter() {
                global_property_reads(globals, value, context, reads);
            }
        }
        ValueExpr::StrictEquality(left, right) => {
            global_property_reads(globals, left, context, reads);
            global_property_reads(globals, right, context, reads);
        }
        ValueExpr::Void(child) | ValueExpr::LogicalNot(child) => {
            global_property_reads(globals, child, context, reads)
        }
        ValueExpr::Conditional { test, alternatives } => {
            global_property_reads(globals, test, context, reads);
            for alternative in alternatives.iter() {
                global_property_reads(globals, alternative, context, reads);
            }
        }
        ValueExpr::Binding(_)
        | ValueExpr::Alloc(..)
        | ValueExpr::Undefined
        | ValueExpr::NonCallable(_)
        | ValueExpr::Unknown => {}
    }
}

#[cfg(test)]
mod tests {
    use super::super::{prepare_callable_capabilities, var_decl_of_item};
    use super::*;
    use raw_js_test_support::parse;

    #[test]
    fn invalidation_revisits_only_aliases_reading_the_changed_global() {
        for width in [1, 16, 128, 512] {
            let mut source = String::new();
            for index in 0..width {
                source.push_str(&format!(
                    "const left{index} = globalThis.leftRegistry; const right{index} = globalThis.rightRegistry;\n"
                ));
            }
            source.push_str("function poison() { globalThis.leftRegistry = globalThis; }");
            let parsed = parse(&source);
            let views = analysis::facts::top_level_item_views(&parsed.body);
            let prepared = prepare_callable_capabilities(
                &views,
                BTreeSet::new(),
                Vec::new(),
                Vec::new(),
                spec::PredefinedGlobals::Standard,
                BundleGlobalNames::default(),
                BTreeMap::new(),
            );
            let modules = vec![Module::prepare(&prepared.index, &[])];
            let candidates = candidate_union(&modules);
            let rules = Rules::new(modules, &candidates);
            let solved = IterativeInference::new(&rules).solve_values_resumable(rules.producers());
            assert!(
                solved
                    .get(&Key::Name(rules.names["leftRegistry"]))
                    .is_some()
            );
            assert!(
                solved
                    .get(&Key::Name(rules.names["rightRegistry"]))
                    .is_none()
            );
            // Every alias and write runs once; only the left aliases run again.
            assert_eq!(solved.evaluations(), 3 * width + 1);
        }
    }

    #[test]
    fn nested_reflected_callees_evaluate_their_shared_receiver_once() {
        struct Counted {
            reads: usize,
        }
        impl IntrinsicRootRead for Counted {
            fn binding_roots(&mut self, _: &Id) -> NativeReceiverRoots {
                self.reads += 1;
                BTreeSet::from([NativeReceiverRoot::Intrinsic(NativeIntrinsic::Object)])
            }
            fn bundle_holds(&mut self, _: Option<&str>, _: &MemberKey) -> bool {
                false
            }
            fn fresh_collections(&mut self) -> bool {
                false
            }
        }
        for depth in [1, 16, 64] {
            let source = format!("const result = alias{};", ".getPrototypeOf()".repeat(depth));
            let parsed = parse(&source);
            let declaration = var_decl_of_item(&parsed.body[0]).unwrap();
            let expression = ValueExpr::from_expr(declaration.decls[0].init.as_ref().unwrap());
            let mut inputs = Counted { reads: 0 };
            let roots = intrinsic_roots_read(
                &expression,
                &mut inputs,
                &GlobalNames::default(),
                &BTreeSet::new(),
                &BTreeMap::new(),
                &CallableReturnInputs::default(),
            );
            assert_eq!(inputs.reads, 1);
            if depth == 1 {
                assert!(roots.contains(&NativeReceiverRoot::Intrinsic(
                    NativeIntrinsic::UnknownGlobalValue
                )));
            } else {
                assert!(roots.is_empty());
            }
        }
    }
}
