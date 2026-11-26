//! Property absence on the intrinsic Object prototype, separate from native call identity.

use super::*;
use analysis_vocabulary::report_schema::{NativePrototypeMutation, NativePrototypeMutationKind};
use std::cell::RefCell;
use swc_common::{DUMMY_SP, SyntaxContext};

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

fn identity_property(property: &str) -> bool {
    matches!(property, "__proto__" | "constructor" | "prototype")
}

// This census uses source-only aliases. Captured value assertions must not
// discharge a source-only prototype absence obligation.
fn prototype_intrinsic_roots(
    value: &ValueExpr,
    aliases: &BTreeMap<Id, NativeReceiverRoots>,
    index: &ExecutionIndex,
    declared: &BTreeSet<Id>,
) -> IntrinsicRoots {
    intrinsic_roots(
        value,
        aliases,
        &index.census_globals,
        declared,
        &BTreeMap::new(),
        &CallableReturnInputs::default(),
    )
    .iter()
    .filter_map(NativeReceiverRoot::intrinsic_root)
    .collect()
}

fn contained_roots(
    value: &ValueExpr,
    aliases: &BTreeMap<Id, NativeReceiverRoots>,
    index: &ExecutionIndex,
    declared: &BTreeSet<Id>,
    callables_by_owner: &BTreeMap<SourceUnitOrdinal, Vec<u32>>,
) -> IntrinsicRoots {
    let mut roots = prototype_intrinsic_roots(value, aliases, index, declared);
    match value {
        ValueExpr::ObjectLiteral(_, properties) => {
            for (_, value) in properties.iter() {
                roots.extend(contained_roots(
                    value,
                    aliases,
                    index,
                    declared,
                    callables_by_owner,
                ));
            }
        }
        ValueExpr::Alloc(Kind::Function, key) => {
            for id in index
                .prototype_callable_reads
                .get(key)
                .into_iter()
                .flatten()
            {
                roots.extend(prototype_intrinsic_roots(
                    &ValueExpr::Binding(id.clone()),
                    aliases,
                    index,
                    declared,
                ));
            }
        }
        ValueExpr::Alloc(Kind::Class, key) => {
            if let Some(owner) = index.class_owners.get(key) {
                for key in callables_by_owner.get(owner).into_iter().flatten() {
                    for id in index
                        .prototype_callable_reads
                        .get(key)
                        .into_iter()
                        .flatten()
                    {
                        roots.extend(prototype_intrinsic_roots(
                            &ValueExpr::Binding(id.clone()),
                            aliases,
                            index,
                            declared,
                        ));
                    }
                }
            }
            if let Some(class) = index.class_values.get(key) {
                for value in class.static_fields.values() {
                    roots.extend(contained_roots(
                        value,
                        aliases,
                        index,
                        declared,
                        callables_by_owner,
                    ));
                }
            }
        }
        ValueExpr::Member(base, property, _, _) => {
            roots.extend(contained_roots(
                base,
                aliases,
                index,
                declared,
                callables_by_owner,
            ));
            if property.as_deref().is_some_and(identity_property) {
                roots.insert(NativeIntrinsic::Object);
            }
        }
        ValueExpr::ArrayIteratorPrototype(expression)
        | ValueExpr::SetIteratorPrototype(expression) => {
            roots.extend(contained_roots(
                expression,
                aliases,
                index,
                declared,
                callables_by_owner,
            ));
        }
        ValueExpr::Call(callee, args, _) | ValueExpr::New(callee, args, _) => {
            roots.extend(contained_roots(
                callee,
                aliases,
                index,
                declared,
                callables_by_owner,
            ));
            for argument in args.iter() {
                roots.extend(contained_roots(
                    argument,
                    aliases,
                    index,
                    declared,
                    callables_by_owner,
                ));
            }
        }
        ValueExpr::Alternatives(_) | ValueExpr::Conditional { .. } | ValueExpr::Sequence(_) => {
            let options = value.result_alternatives().expect("alternative expression");
            for option in options {
                roots.extend(contained_roots(
                    option,
                    aliases,
                    index,
                    declared,
                    callables_by_owner,
                ));
            }
        }
        _ => {}
    }
    // Reflect does not have a family in the native-call catalogue, but is a
    // descriptor capability when it is passed or destructured.
    if matches!(value, ValueExpr::Binding(id) if id.0.as_ref() == "Reflect") {
        roots.insert(NativeIntrinsic::Object);
    }
    roots
}

#[derive(Clone)]
enum RootTransfer {
    Empty,
    Static(NativeReceiverRoots),
    /// One operation with structural children replaced by graph-local binding
    /// leaves. `intrinsic_roots_read` remains the authority for its transfer.
    Compiled(ValueExpr),
}

fn capability_node_binding(node: usize) -> Id {
    // A NUL cannot occur in a JavaScript binding name. These IDs live only in
    // the compiled graph and are resolved before source bindings are read.
    (
        format!("\0debundle-capability-node-{node}").into(),
        SyntaxContext::empty(),
    )
}

#[derive(Clone, Copy)]
enum ContainedInput {
    Nested(usize),
    Roots(usize),
}

struct CapabilityNode {
    roots: RootTransfer,
    root_inputs: Vec<usize>,
    contained: Vec<ContainedInput>,
    extra: IntrinsicRoots,
}

struct CapabilityGraph {
    nodes: Vec<CapabilityNode>,
    parents: Vec<Vec<usize>>,
    node_bindings: BTreeMap<Id, usize>,
    root: usize,
    dependencies: BTreeSet<Id>,
    alias_nodes: BTreeMap<Id, Vec<usize>>,
    #[cfg(test)]
    owner_callable_visits: usize,
}

#[derive(Default)]
struct CapabilityGraphState {
    aliases: BTreeMap<Id, NativeReceiverRoots>,
    roots: Vec<NativeReceiverRoots>,
    contained: Vec<IntrinsicRoots>,
    proposed: IntrinsicRoots,
    initialized: bool,
    #[cfg(test)]
    transfer_evaluations: std::rc::Rc<std::cell::Cell<usize>>,
}

struct CapabilityGraphCompiler<'a> {
    index: &'a ExecutionIndex,
    declared: &'a BTreeSet<Id>,
    callables_by_owner: &'a BTreeMap<SourceUnitOrdinal, Vec<u32>>,
    nodes: Vec<CapabilityNode>,
    node_bindings: BTreeMap<Id, usize>,
    dependencies: BTreeSet<Id>,
    alias_nodes: BTreeMap<Id, Vec<usize>>,
    #[cfg(test)]
    owner_callable_visits: usize,
}

impl CapabilityGraphCompiler<'_> {
    fn add_node(
        &mut self,
        roots: RootTransfer,
        root_inputs: Vec<usize>,
        contained: Vec<ContainedInput>,
        extra: IntrinsicRoots,
    ) -> usize {
        let id = self.nodes.len();
        self.nodes.push(CapabilityNode {
            roots,
            root_inputs,
            contained,
            extra,
        });
        self.node_bindings.insert(capability_node_binding(id), id);
        id
    }

    fn node_value(&self, id: usize) -> ValueExpr {
        ValueExpr::Binding(capability_node_binding(id))
    }

    fn binding(&mut self, id: &Id) -> usize {
        self.dependencies.insert(id.clone());
        let value = ValueExpr::Binding(id.clone());
        let static_roots =
            prototype_intrinsic_roots(&value, &BTreeMap::new(), self.index, self.declared);
        let roots = if !self.declared.contains(id) && !static_roots.is_empty() {
            RootTransfer::Static(
                static_roots
                    .into_iter()
                    .map(NativeReceiverRoot::Intrinsic)
                    .collect(),
            )
        } else {
            let node = self.nodes.len();
            self.alias_nodes.entry(id.clone()).or_default().push(node);
            RootTransfer::Compiled(value)
        };
        let extra = if id.0.as_ref() == "Reflect" {
            IntrinsicRoots::from([NativeIntrinsic::Object])
        } else {
            IntrinsicRoots::new()
        };
        self.add_node(roots, vec![], vec![], extra)
    }

    fn member(
        &mut self,
        base: &ValueExpr,
        property: &Option<String>,
        key: &MemberKey,
        site: &CallSite,
    ) -> (usize, bool, usize) {
        let (base, has_shadowed_root) = self.value(base);
        let mut extra = IntrinsicRoots::new();
        if property.as_deref().is_some_and(identity_property) {
            extra.insert(NativeIntrinsic::Object);
        }
        let node = self.add_node(
            RootTransfer::Compiled(ValueExpr::Member(
                Arc::new(self.node_value(base)),
                property.clone(),
                key.clone(),
                *site,
            )),
            vec![base],
            vec![ContainedInput::Nested(base)],
            extra,
        );
        (node, has_shadowed_root, base)
    }

    fn value(&mut self, value: &ValueExpr) -> (usize, bool) {
        match value {
            ValueExpr::Binding(id) => (
                self.binding(id),
                self.declared.contains(id) && matches!(id.0.as_ref(), "Object" | "Symbol"),
            ),
            ValueExpr::Alloc(Kind::Function, key) => {
                let contained = self
                    .index
                    .prototype_callable_reads
                    .get(key)
                    .into_iter()
                    .flatten()
                    .map(|id| ContainedInput::Roots(self.binding(id)))
                    .collect();
                (
                    self.add_node(
                        RootTransfer::Empty,
                        vec![],
                        contained,
                        IntrinsicRoots::new(),
                    ),
                    false,
                )
            }
            ValueExpr::Alloc(Kind::Class, key) => {
                let mut contained = Vec::new();
                if let Some(owner) = self.index.class_owners.get(key)
                    && let Some(callables) = self.callables_by_owner.get(owner)
                {
                    for callable in callables {
                        #[cfg(test)]
                        {
                            self.owner_callable_visits += 1;
                        }
                        for id in self
                            .index
                            .prototype_callable_reads
                            .get(callable)
                            .into_iter()
                            .flatten()
                        {
                            contained.push(ContainedInput::Roots(self.binding(id)));
                        }
                    }
                }
                if let Some(class) = self.index.class_values.get(key) {
                    for value in class.static_fields.values() {
                        let (child, _) = self.value(value);
                        contained.push(ContainedInput::Nested(child));
                    }
                }
                (
                    self.add_node(
                        RootTransfer::Empty,
                        vec![],
                        contained,
                        IntrinsicRoots::new(),
                    ),
                    false,
                )
            }
            ValueExpr::ObjectLiteral(key, properties) => {
                let mut compiled_fields = Vec::with_capacity(properties.len());
                let mut root_inputs = Vec::with_capacity(properties.len());
                let mut contained = Vec::with_capacity(properties.len());
                let mut has_shadowed_root = false;
                for (name, value) in properties.iter() {
                    let (child, child_has_shadowed_root) = self.value(value);
                    compiled_fields.push((name.clone(), self.node_value(child)));
                    root_inputs.push(child);
                    contained.push(ContainedInput::Nested(child));
                    has_shadowed_root |= child_has_shadowed_root;
                }
                (
                    self.add_node(
                        RootTransfer::Compiled(ValueExpr::ObjectLiteral(
                            *key,
                            compiled_fields.into(),
                        )),
                        root_inputs,
                        contained,
                        IntrinsicRoots::new(),
                    ),
                    has_shadowed_root,
                )
            }
            ValueExpr::Member(base, property, key, site) => {
                let (node, shadowed, _) = self.member(base, property, key, site);
                (node, shadowed)
            }
            ValueExpr::ArrayIteratorPrototype(expression) => {
                let (expression, has_shadowed_root) = self.value(expression);
                let roots = if has_shadowed_root {
                    RootTransfer::Static(BTreeSet::from([NativeReceiverRoot::Intrinsic(
                        NativeIntrinsic::UnknownGlobalValue,
                    )]))
                } else {
                    RootTransfer::Static(BTreeSet::from([
                        NativeReceiverRoot::ArrayIteratorPrototype,
                    ]))
                };
                (
                    self.add_node(
                        roots,
                        vec![],
                        vec![ContainedInput::Nested(expression)],
                        IntrinsicRoots::new(),
                    ),
                    has_shadowed_root,
                )
            }
            ValueExpr::SetIteratorPrototype(expression) => {
                let (expression, has_shadowed_root) = self.value(expression);
                (
                    self.add_node(
                        RootTransfer::Static(BTreeSet::from([
                            NativeReceiverRoot::SetIteratorPrototype,
                        ])),
                        vec![],
                        vec![ContainedInput::Nested(expression)],
                        IntrinsicRoots::new(),
                    ),
                    has_shadowed_root,
                )
            }
            ValueExpr::Alternatives(options) => self.alternatives(options.iter()),
            ValueExpr::Sequence(options) => {
                let mut compiled_options = Vec::with_capacity(options.len());
                let mut contained = Vec::new();
                let mut has_shadowed_root = false;
                let mut final_value = None;
                for value in options.iter() {
                    let (child, child_has_shadowed_root) = self.value(value);
                    has_shadowed_root |= child_has_shadowed_root;
                    final_value = Some((child, value));
                    compiled_options.push(self.alternative_value(child, value));
                }
                let root_inputs = final_value
                    .filter(|(_, value)| intrinsic_alternative_can_be_unknown(value))
                    .map(|(child, _)| vec![child])
                    .unwrap_or_default();
                if let Some((child, _)) = final_value {
                    contained.push(ContainedInput::Nested(child));
                }
                (
                    self.add_node(
                        RootTransfer::Compiled(ValueExpr::Sequence(compiled_options.into())),
                        root_inputs,
                        contained,
                        IntrinsicRoots::new(),
                    ),
                    has_shadowed_root,
                )
            }
            ValueExpr::Conditional { test, alternatives } => {
                let (test, test_has_shadowed_root) = self.value(test);
                let mut compiled_alternatives = Vec::with_capacity(2);
                let mut root_inputs = Vec::with_capacity(2);
                let mut contained = Vec::with_capacity(2);
                let mut alternatives_have_shadowed_root = false;
                for value in alternatives.iter() {
                    let (child, child_has_shadowed_root) = self.value(value);
                    compiled_alternatives.push(self.alternative_value(child, value));
                    if intrinsic_alternative_can_be_unknown(value) {
                        root_inputs.push(child);
                    }
                    contained.push(ContainedInput::Nested(child));
                    alternatives_have_shadowed_root |= child_has_shadowed_root;
                }
                let has_shadowed_root = test_has_shadowed_root || alternatives_have_shadowed_root;
                let compiled_alternatives: Arc<[ValueExpr; 2]> = Arc::new([
                    compiled_alternatives.remove(0),
                    compiled_alternatives.remove(0),
                ]);
                let node = self.add_node(
                    RootTransfer::Compiled(ValueExpr::Conditional {
                        test: Arc::new(self.node_value(test)),
                        alternatives: compiled_alternatives,
                    }),
                    root_inputs,
                    contained,
                    IntrinsicRoots::new(),
                );
                (node, has_shadowed_root)
            }
            ValueExpr::Call(callee, args, site) | ValueExpr::New(callee, args, site) => {
                let (callee_node, compiled_callee, mut root_inputs, mut has_shadowed_root) =
                    match callee.as_ref() {
                        ValueExpr::Member(base, Some(property), key, member_site)
                            if property == "getPrototypeOf" && key.is_static() =>
                        {
                            let (member_node, has_shadowed_root, base_node) =
                                self.member(base, &Some(property.clone()), key, member_site);
                            (
                                member_node,
                                ValueExpr::Member(
                                    Arc::new(self.node_value(base_node)),
                                    Some(property.clone()),
                                    key.clone(),
                                    *member_site,
                                ),
                                vec![base_node],
                                has_shadowed_root,
                            )
                        }
                        callee => {
                            let (callee_node, has_shadowed_root) = self.value(callee);
                            (
                                callee_node,
                                self.node_value(callee_node),
                                vec![callee_node],
                                has_shadowed_root,
                            )
                        }
                    };
                let mut contained = vec![ContainedInput::Nested(callee_node)];
                let mut compiled_args = Vec::with_capacity(args.len());
                for argument in args.iter() {
                    let (argument, child_has_shadowed_root) = self.value(argument);
                    has_shadowed_root |= child_has_shadowed_root;
                    root_inputs.push(argument);
                    contained.push(ContainedInput::Nested(argument));
                    compiled_args.push(self.node_value(argument));
                }
                let compiled_value = if matches!(value, ValueExpr::Call(..)) {
                    ValueExpr::Call(Arc::new(compiled_callee), compiled_args.into(), *site)
                } else {
                    ValueExpr::New(Arc::new(compiled_callee), compiled_args.into(), *site)
                };
                (
                    self.add_node(
                        RootTransfer::Compiled(compiled_value),
                        root_inputs,
                        contained,
                        IntrinsicRoots::new(),
                    ),
                    has_shadowed_root,
                )
            }
            ValueExpr::Primitive(operands) => {
                let mut has_shadowed_root = false;
                for operand in operands.iter() {
                    has_shadowed_root |= self.value(operand).1;
                }
                (
                    self.add_node(RootTransfer::Empty, vec![], vec![], IntrinsicRoots::new()),
                    has_shadowed_root,
                )
            }
            ValueExpr::StrictEquality(left, right) => {
                let has_shadowed_root = self.value(left).1 | self.value(right).1;
                (
                    self.add_node(RootTransfer::Empty, vec![], vec![], IntrinsicRoots::new()),
                    has_shadowed_root,
                )
            }
            ValueExpr::LogicalNot(expression) | ValueExpr::Void(expression) => {
                let has_shadowed_root = self.value(expression).1;
                (
                    self.add_node(RootTransfer::Empty, vec![], vec![], IntrinsicRoots::new()),
                    has_shadowed_root,
                )
            }
            ValueExpr::Undefined
            | ValueExpr::NonCallable(_)
            | ValueExpr::Unknown
            | ValueExpr::Alloc(..) => (
                self.add_node(RootTransfer::Empty, vec![], vec![], IntrinsicRoots::new()),
                false,
            ),
        }
    }

    fn alternatives<'b>(&mut self, values: impl Iterator<Item = &'b ValueExpr>) -> (usize, bool) {
        let mut options = Vec::new();
        let mut root_inputs = Vec::new();
        let mut contained = Vec::new();
        let mut has_shadowed_root = false;
        for value in values {
            let (child, child_has_shadowed_root) = self.value(value);
            options.push(self.alternative_value(child, value));
            if intrinsic_alternative_can_be_unknown(value) {
                root_inputs.push(child);
            }
            contained.push(ContainedInput::Nested(child));
            has_shadowed_root |= child_has_shadowed_root;
        }
        (
            self.add_node(
                RootTransfer::Compiled(ValueExpr::Alternatives(options.into())),
                root_inputs,
                contained,
                IntrinsicRoots::new(),
            ),
            has_shadowed_root,
        )
    }

    fn alternative_value(&self, child: usize, source: &ValueExpr) -> ValueExpr {
        if intrinsic_alternative_can_be_unknown(source) {
            self.node_value(child)
        } else {
            // These source forms always have an empty intrinsic-root transfer;
            // keep that fact so the shared alternatives rule does not treat
            // their empty result as an unknown arm.
            ValueExpr::Undefined
        }
    }

    fn finish(self, root: usize) -> CapabilityGraph {
        let mut parents = vec![Vec::new(); self.nodes.len()];
        for (parent, node) in self.nodes.iter().enumerate() {
            for child in &node.root_inputs {
                parents[*child].push(parent);
            }
            for input in &node.contained {
                let child = match input {
                    ContainedInput::Nested(child) | ContainedInput::Roots(child) => *child,
                };
                parents[child].push(parent);
            }
        }
        for nodes in &mut parents {
            nodes.sort_unstable();
            nodes.dedup();
        }
        CapabilityGraph {
            nodes: self.nodes,
            parents,
            node_bindings: self.node_bindings,
            root,
            dependencies: self.dependencies,
            alias_nodes: self.alias_nodes,
            #[cfg(test)]
            owner_callable_visits: self.owner_callable_visits,
        }
    }
}

struct CapabilityGraphPlan {
    target: Id,
    graph: CapabilityGraph,
    state: RefCell<CapabilityGraphState>,
}

fn compile_capability_plan(
    index: &ExecutionIndex,
    declared: &BTreeSet<Id>,
    callables_by_owner: &BTreeMap<SourceUnitOrdinal, Vec<u32>>,
    target: Id,
    value: &ValueExpr,
) -> CapabilityGraphPlan {
    let mut compiler = CapabilityGraphCompiler {
        index,
        declared,
        callables_by_owner,
        nodes: Vec::new(),
        node_bindings: BTreeMap::new(),
        dependencies: BTreeSet::new(),
        alias_nodes: BTreeMap::new(),
        #[cfg(test)]
        owner_callable_visits: 0,
    };
    let (root, _) = compiler.value(value);
    CapabilityGraphPlan {
        target,
        graph: compiler.finish(root),
        state: RefCell::default(),
    }
}

struct CapabilityGraphRootInputs<'a> {
    aliases: &'a BTreeMap<Id, NativeReceiverRoots>,
    roots: &'a [NativeReceiverRoots],
    node_bindings: &'a BTreeMap<Id, usize>,
    bundle: &'a BundleGlobals,
}

impl IntrinsicRootRead for CapabilityGraphRootInputs<'_> {
    fn binding_roots(&mut self, binding: &Id) -> NativeReceiverRoots {
        self.node_bindings
            .get(binding)
            .map(|node| self.roots[*node].clone())
            .or_else(|| self.aliases.get(binding).cloned())
            .unwrap_or_default()
    }

    fn bundle_holds(&mut self, property: Option<&str>, key: &MemberKey) -> bool {
        self.bundle.holds(property, key)
    }

    fn fresh_collections(&mut self) -> bool {
        self.bundle.fresh_collections
    }
}

impl CapabilityGraph {
    fn transfer_roots(
        &self,
        node: usize,
        state: &CapabilityGraphState,
        index: &ExecutionIndex,
        declared: &BTreeSet<Id>,
    ) -> NativeReceiverRoots {
        match &self.nodes[node].roots {
            RootTransfer::Empty => NativeReceiverRoots::new(),
            RootTransfer::Static(roots) => roots.clone(),
            RootTransfer::Compiled(value) => {
                let mut inputs = CapabilityGraphRootInputs {
                    aliases: &state.aliases,
                    roots: &state.roots,
                    node_bindings: &self.node_bindings,
                    bundle: &index.census_globals.bundle,
                };
                intrinsic_roots_read(
                    value,
                    &mut inputs,
                    &index.census_globals,
                    declared,
                    &BTreeMap::new(),
                    &CallableReturnInputs::default(),
                )
            }
        }
    }

    fn recompute_node(
        &self,
        node: usize,
        state: &mut CapabilityGraphState,
        index: &ExecutionIndex,
        declared: &BTreeSet<Id>,
    ) -> bool {
        #[cfg(test)]
        {
            state
                .transfer_evaluations
                .set(state.transfer_evaluations.get() + 1);
        }
        let roots = self.transfer_roots(node, state, index, declared);
        let roots_changed = state.roots[node] != roots;
        state.roots[node] = roots;

        let intrinsic_roots = |index: usize| {
            state.roots[index]
                .iter()
                .filter_map(NativeReceiverRoot::intrinsic_root)
        };
        let mut contained = self.nodes[node].extra.clone();
        contained.extend(intrinsic_roots(node));
        for input in &self.nodes[node].contained {
            match input {
                ContainedInput::Nested(child) => {
                    contained.extend(state.contained[*child].iter().copied());
                }
                ContainedInput::Roots(child) => contained.extend(intrinsic_roots(*child)),
            }
        }
        let contained_changed = state.contained[node] != contained;
        state.contained[node] = contained;
        roots_changed || contained_changed
    }

    fn recompute_all(
        &self,
        state: &mut CapabilityGraphState,
        index: &ExecutionIndex,
        declared: &BTreeSet<Id>,
    ) {
        for node in 0..self.nodes.len() {
            self.recompute_node(node, state, index, declared);
        }
    }

    fn recompute_changed(
        &self,
        changed: impl IntoIterator<Item = usize>,
        state: &mut CapabilityGraphState,
        index: &ExecutionIndex,
        declared: &BTreeSet<Id>,
    ) {
        let mut pending = changed.into_iter().collect::<BTreeSet<_>>();
        while let Some(node) = pending.pop_first() {
            if self.recompute_node(node, state, index, declared) {
                pending.extend(self.parents[node].iter().copied());
            }
        }
    }
}

struct CapabilityContainmentRules<'a> {
    plans: Vec<CapabilityGraphPlan>,
    index: &'a ExecutionIndex,
    declared: &'a BTreeSet<Id>,
}

impl CapabilityContainmentRules<'_> {
    fn propose_roots(
        plan: &CapabilityGraphPlan,
        state: &mut CapabilityGraphState,
        context: &mut impl FactContext<Id, NativeReceiverRoots>,
    ) {
        let roots = &state.contained[plan.graph.root];
        if roots.is_empty() {
            return;
        }
        let contribution = roots
            .difference(&state.proposed)
            .copied()
            .map(NativeReceiverRoot::Intrinsic)
            .collect();
        context.propose(plan.target.clone(), contribution);
        state.proposed.extend(roots.iter().copied());
    }
}

impl IterativeRules for CapabilityContainmentRules<'_> {
    type Key = Id;
    type Fact = NativeReceiverRoots;
    type Rule = usize;
    type MergeScratch = ();
    const EVALUATES_CHANGES: bool = true;

    fn evaluate(&self, rule: &Self::Rule, context: &mut impl FactContext<Self::Key, Self::Fact>) {
        let plan = &self.plans[*rule];
        let mut state = plan.state.borrow_mut();
        assert!(!state.initialized, "a capability rule starts once");
        for id in &plan.graph.dependencies {
            if let Some(roots) = context.read(id.clone()).cloned() {
                state.aliases.insert(id.clone(), roots);
            }
        }
        state
            .roots
            .resize_with(plan.graph.nodes.len(), BTreeSet::new);
        state
            .contained
            .resize_with(plan.graph.nodes.len(), IntrinsicRoots::new);
        plan.graph
            .recompute_all(&mut state, self.index, self.declared);
        state.initialized = true;
        Self::propose_roots(plan, &mut state, context);
    }

    fn evaluate_changed(
        &self,
        rule: &Self::Rule,
        changed: &BTreeSet<Self::Key>,
        context: &mut impl FactContext<Self::Key, Self::Fact>,
    ) {
        let plan = &self.plans[*rule];
        let values = changed
            .iter()
            .filter_map(|id| {
                context
                    .read(id.clone())
                    .cloned()
                    .map(|roots| (id.clone(), roots))
            })
            .collect::<BTreeMap<_, _>>();
        let mut state = plan.state.borrow_mut();
        assert!(
            state.initialized,
            "changed capability rules already started"
        );
        let mut nodes = Vec::new();
        for (id, roots) in values {
            state.aliases.insert(id.clone(), roots);
            nodes.extend(
                plan.graph
                    .alias_nodes
                    .get(&id)
                    .into_iter()
                    .flatten()
                    .copied(),
            );
        }
        plan.graph
            .recompute_changed(nodes, &mut state, self.index, self.declared);
        Self::propose_roots(plan, &mut state, context);
    }

    fn merge(_: &Self::Key, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
        let previous_len = current.len();
        current.extend(contribution);
        current.len() != previous_len
    }
}

impl ExecutionIndex {
    pub(super) fn collect_prototype_mutations(
        &self,
        aliases: &BTreeMap<Id, NativeReceiverRoots>,
        assignments: &[&(Id, ValueExpr)],
        declared: &BTreeSet<Id>,
    ) -> Vec<NativePrototypeMutation> {
        // Capability containment is separate from receiver identity: a record
        // holding a native is not itself a native mutation receiver.
        let bindings = assignments
            .iter()
            .copied()
            .map(|(id, value)| (id.clone(), value.clone()))
            .chain(
                self.hoisted_bindings
                    .iter()
                    .map(|(id, key)| (id.clone(), ValueExpr::Alloc(Kind::Function, *key))),
            )
            .chain(
                self.class_bindings
                    .iter()
                    .map(|(key, id)| (id.clone(), ValueExpr::Alloc(Kind::Class, *key))),
            )
            .collect::<Vec<_>>();
        let producer_count = bindings.len();
        let mut callables_by_owner = BTreeMap::<SourceUnitOrdinal, Vec<u32>>::new();
        for (key, callable) in &self.callables {
            callables_by_owner
                .entry(callable.owner)
                .or_default()
                .push(*key);
        }
        let plans = bindings
            .into_iter()
            .map(|(target, value)| {
                compile_capability_plan(self, declared, &callables_by_owner, target, &value)
            })
            .collect();
        let mut inference = IterativeInference::new(CapabilityContainmentRules {
            plans,
            index: self,
            declared,
        });
        for (id, roots) in aliases {
            inference.seed(id.clone(), roots.clone());
        }
        let capabilities = inference.solve(0..producer_count).into_values();
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
        for (owner, _, span, key) in &self.prototype_field_writes {
            mutations.push(mutation(
                *owner,
                *span,
                NativePrototypeMutationKind::Write,
                Some(key),
            ));
        }
        // A native descriptor capability can escape through an alias, bind/call,
        // an imported helper, or a legacy accessor method. Such paths are not
        // closed by the older native-family mutation census.
        for call in &self.prototype_calls {
            // Prototype/constructor paths on otherwise untracked receivers
            // can recover a native without naming a builtin. Retain the read
            // even inside a deferred closure or an unindexed accessor body.
            if let ValueExpr::Member(base, Some(property), _, _) = &call.callee
                && identity_property(property)
                && prototype_intrinsic_roots(base, aliases, self, declared).is_empty()
            {
                mutations.push(mutation(
                    call.owner,
                    call.span,
                    NativePrototypeMutationKind::Escape,
                    None,
                ));
                continue;
            }
            let member = match &call.callee {
                ValueExpr::Member(_, property, _, _) => Some(property),
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
                let roots =
                    contained_roots(argument, &capabilities, self, declared, &callables_by_owner);
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

#[cfg(test)]
mod capability_graph_tests {
    use super::*;
    use analysis_vocabulary::inputs::ValueInputRef;
    use std::rc::Rc;
    use swc_common::SyntaxContext;

    fn id(name: &str) -> Id {
        (name.into(), SyntaxContext::empty())
    }

    fn object(values: impl IntoIterator<Item = ValueExpr>) -> ValueExpr {
        ValueExpr::ObjectLiteral(
            1,
            values
                .into_iter()
                .enumerate()
                .map(|(index, value)| (Some(format!("field{index}")), value))
                .collect::<Vec<_>>()
                .into(),
        )
    }

    fn compile(
        value: &ValueExpr,
        index: &ExecutionIndex,
        declared: &BTreeSet<Id>,
    ) -> CapabilityGraph {
        compile_capability_plan(index, declared, &BTreeMap::new(), id("target"), value).graph
    }

    fn evaluate(
        graph: &CapabilityGraph,
        aliases: BTreeMap<Id, NativeReceiverRoots>,
        index: &ExecutionIndex,
        declared: &BTreeSet<Id>,
    ) -> CapabilityGraphState {
        let mut state = CapabilityGraphState {
            aliases,
            ..CapabilityGraphState::default()
        };
        state.roots.resize_with(graph.nodes.len(), BTreeSet::new);
        state
            .contained
            .resize_with(graph.nodes.len(), IntrinsicRoots::new);
        graph.recompute_all(&mut state, index, declared);
        state
    }

    #[test]
    fn compiled_transfers_match_the_recursive_source_projection() {
        let source = id("source");
        let unknown = id("unknown");
        let declared = BTreeSet::from([source.clone(), unknown.clone()]);
        let index = ExecutionIndex::default();
        let aliases = BTreeMap::from([(
            source.clone(),
            BTreeSet::from([NativeReceiverRoot::OrdinaryDirect {
                reference: ValueInputRef {
                    defining_chunk: "provider".into(),
                    input_index: 0,
                },
                available_after: SourceUnitOrdinal(0),
            }]),
        )]);
        let site = CallSite::from(DUMMY_SP);
        let values = [
            object([
                ValueExpr::Binding(source.clone()),
                ValueExpr::Binding(id("Object")),
            ]),
            ValueExpr::Member(
                Arc::new(ValueExpr::Binding(id("Object"))),
                Some("__proto__".into()),
                MemberKey::Static,
                site,
            ),
            ValueExpr::Call(
                Arc::new(ValueExpr::Member(
                    Arc::new(ValueExpr::Binding(id("Object"))),
                    Some("getPrototypeOf".into()),
                    MemberKey::Static,
                    site,
                )),
                vec![ValueExpr::Binding(source.clone())].into(),
                site,
            ),
            ValueExpr::New(
                Arc::new(ValueExpr::Binding(id("Object"))),
                vec![ValueExpr::Binding(source.clone())].into(),
                site,
            ),
            ValueExpr::Alternatives(
                vec![
                    ValueExpr::Binding(unknown),
                    ValueExpr::Binding(source.clone()),
                ]
                .into(),
            ),
            ValueExpr::Sequence(
                vec![
                    ValueExpr::Binding(id("Object")),
                    ValueExpr::Binding(source.clone()),
                ]
                .into(),
            ),
            ValueExpr::Conditional {
                test: Arc::new(ValueExpr::Binding(source.clone())),
                alternatives: Arc::new([
                    ValueExpr::Binding(id("Object")),
                    ValueExpr::Binding(source.clone()),
                ]),
            },
            ValueExpr::ArrayIteratorPrototype(Arc::new(ValueExpr::Binding(id("Object")))),
            ValueExpr::SetIteratorPrototype(Arc::new(ValueExpr::Binding(source.clone()))),
        ];

        for value in values {
            let graph = compile(&value, &index, &declared);
            let state = evaluate(&graph, aliases.clone(), &index, &declared);
            let expected = contained_roots(&value, &aliases, &index, &declared, &BTreeMap::new());
            assert_eq!(
                state.contained[graph.root], expected,
                "compiled containment changed for {value:?}"
            );
        }
    }

    #[test]
    fn a_changed_alias_recomputes_each_dependent_node_once() {
        let source = id("source");
        let depth = 128;
        let fanout = 96;
        let chain = (0..depth).fold(ValueExpr::Binding(source.clone()), |value, _| {
            object([value])
        });
        let shared_result =
            object((0..fanout).map(|_| object([ValueExpr::Binding(source.clone())])));
        let index = ExecutionIndex::default();
        let declared = BTreeSet::from([source.clone()]);

        for (value, node_count, why) in [
            (
                chain,
                depth + 1,
                "one changed leaf touches each node on its only containment path once",
            ),
            (
                shared_result,
                2 * fanout + 1,
                "shared alias growth visits its fanout and the common result once",
            ),
        ] {
            let graph = compile(&value, &index, &declared);
            let roots = BTreeSet::from([NativeReceiverRoot::Intrinsic(NativeIntrinsic::Array)]);
            let mut state = evaluate(
                &graph,
                BTreeMap::from([(source.clone(), roots)]),
                &index,
                &declared,
            );
            assert_eq!(graph.nodes.len(), node_count, "{why}");
            assert_eq!(state.transfer_evaluations.get(), graph.nodes.len(), "{why}");

            state.aliases.insert(
                source.clone(),
                BTreeSet::from([
                    NativeReceiverRoot::Intrinsic(NativeIntrinsic::Array),
                    NativeReceiverRoot::Intrinsic(NativeIntrinsic::Object),
                ]),
            );
            let before = state.transfer_evaluations.get();
            graph.recompute_changed(
                graph.alias_nodes[&source].iter().copied(),
                &mut state,
                &index,
                &declared,
            );
            assert_eq!(
                state.transfer_evaluations.get() - before,
                graph.nodes.len(),
                "{why}"
            );
            assert_eq!(
                state.contained[graph.root],
                BTreeSet::from([NativeIntrinsic::Array, NativeIntrinsic::Object]),
                "{why}"
            );
        }
    }

    #[test]
    fn empty_alternative_reasons_shrink_locally_but_emitted_facts_remain_joined() {
        let unknown = id("unknown");
        let ordinary = id("ordinary");
        let value = ValueExpr::Alternatives(
            vec![
                ValueExpr::Binding(unknown.clone()),
                ValueExpr::Binding(ordinary.clone()),
            ]
            .into(),
        );
        let index = ExecutionIndex::default();
        let declared = BTreeSet::from([unknown.clone(), ordinary.clone()]);
        let graph = compile(&value, &index, &declared);
        let ordinary_root = NativeReceiverRoot::OrdinaryDirect {
            reference: ValueInputRef {
                defining_chunk: "provider".into(),
                input_index: 0,
            },
            available_after: SourceUnitOrdinal(0),
        };
        let mut state = evaluate(
            &graph,
            BTreeMap::from([(ordinary.clone(), BTreeSet::from([ordinary_root]))]),
            &index,
            &declared,
        );
        let old = contained_roots(&value, &state.aliases, &index, &declared, &BTreeMap::new());
        assert_eq!(state.contained[graph.root], old);
        assert_eq!(old, BTreeSet::from([NativeIntrinsic::UnknownGlobalValue]));
        let mut emitted = old;

        state.aliases.insert(
            unknown.clone(),
            BTreeSet::from([NativeReceiverRoot::Intrinsic(NativeIntrinsic::Array)]),
        );
        graph.recompute_changed(
            graph.alias_nodes[&unknown].iter().copied(),
            &mut state,
            &index,
            &declared,
        );
        let current = contained_roots(&value, &state.aliases, &index, &declared, &BTreeMap::new());
        assert_eq!(state.contained[graph.root], current);
        emitted.extend(current);
        assert_eq!(
            emitted,
            BTreeSet::from([NativeIntrinsic::Array, NativeIntrinsic::UnknownGlobalValue]),
            "the rule target joins prior conservative output while each compiled node reflects current inputs"
        );
    }

    #[test]
    fn class_containment_visits_only_callables_for_its_indexed_owner() {
        let owner = SourceUnitOrdinal(7);
        let mut index = ExecutionIndex::default();
        index.class_owners.insert(42, owner);
        for key in 0..1000_usize {
            let callable = Callable {
                owner: if key == 0 {
                    owner
                } else {
                    SourceUnitOrdinal(key + 100)
                },
                ..Callable::default()
            };
            index.callables.insert(
                u32::try_from(key).expect("test callable key fits u32"),
                callable,
            );
        }
        index
            .prototype_callable_reads
            .insert(0, BTreeSet::from([id("Object")]));
        let mut class_value = ClassValueFacts::default();
        class_value
            .static_fields
            .insert("record".into(), object([ValueExpr::Binding(id("Array"))]));
        index.class_values.insert(42, class_value);
        let mut callables_by_owner = BTreeMap::<SourceUnitOrdinal, Vec<u32>>::new();
        for (key, callable) in &index.callables {
            callables_by_owner
                .entry(callable.owner)
                .or_default()
                .push(*key);
        }
        let declared = BTreeSet::new();
        let graph = compile_capability_plan(
            &index,
            &declared,
            &callables_by_owner,
            id("Bridge"),
            &ValueExpr::Alloc(Kind::Class, 42),
        )
        .graph;
        assert_eq!(graph.owner_callable_visits, 1);

        let state = evaluate(&graph, BTreeMap::new(), &index, &declared);
        assert_eq!(
            state.contained[graph.root],
            BTreeSet::from([NativeIntrinsic::Object, NativeIntrinsic::Array])
        );
    }

    #[test]
    fn cyclic_assignment_closure_propagates_roots_with_bounded_graph_work() {
        let first = id("first");
        let second = id("second");
        let declared = BTreeSet::from([first.clone(), second.clone()]);
        let assignments = [
            (
                first.clone(),
                object([
                    ValueExpr::Binding(id("Object")),
                    ValueExpr::Binding(second.clone()),
                ]),
            ),
            (second.clone(), object([ValueExpr::Binding(first.clone())])),
        ];
        let index = ExecutionIndex::default();
        let callables_by_owner = BTreeMap::new();
        let plans = assignments
            .iter()
            .map(|(target, value)| {
                compile_capability_plan(
                    &index,
                    &declared,
                    &callables_by_owner,
                    target.clone(),
                    value,
                )
            })
            .collect::<Vec<_>>();
        let structural_nodes = plans
            .iter()
            .map(|plan| plan.graph.nodes.len())
            .sum::<usize>();
        let evaluations = plans
            .iter()
            .map(|plan| Rc::clone(&plan.state.borrow().transfer_evaluations))
            .collect::<Vec<_>>();
        let facts = IterativeInference::new(CapabilityContainmentRules {
            plans,
            index: &index,
            declared: &declared,
        })
        .solve(0..assignments.len())
        .into_values();
        let object = NativeReceiverRoot::Intrinsic(NativeIntrinsic::Object);
        assert!(facts[&first].contains(&object));
        assert!(facts[&second].contains(&object));
        let evaluated = evaluations.iter().map(|count| count.get()).sum::<usize>();
        assert!(
            evaluated <= 2 * structural_nodes,
            "one intrinsic token crosses the cyclic binding graph in at most two node passes: {evaluated} evaluations for {structural_nodes} nodes"
        );
    }
}
