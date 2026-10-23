//! Monotone, intra-chunk value identities for local calls and object aliases.
//!
//! The sets deliberately over-approximate: an unsupported expression yields
//! `Unknown`, and every assignment adds possible values rather than replacing
//! them. The same facts feed at-init call promotion and PlainData's escape
//! check, so an alias/return cannot be understood by one pass and invisible to
//! the other. This is context-insensitive; it makes no claim about values from
//! other chunks.

use std::collections::{BTreeMap, BTreeSet, VecDeque};

use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

use super::{
    LazyBoundary, PositionBucketed, StatementFacts, TopLevelItemView, lazy_visit_arrow_expr,
    lazy_visit_bin_expr, lazy_visit_class, lazy_visit_class_member, lazy_visit_cond_expr,
    lazy_visit_function, lazy_visit_getter_prop, lazy_visit_if_stmt, lazy_visit_may_skip,
    lazy_visit_method_prop, lazy_visit_setter_prop,
};
use crate::StatementOrdinal;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) enum ValueOrigin {
    Function(u32),
    Class(u32),
    Object(u32),
    Unknown,
}

type Values = BTreeSet<ValueOrigin>;

/// Candidate-object reachability for repeated escape checks. The value-flow
/// graph is immutable after analysis, so propagate each candidate backwards
/// through return/property edges once instead of walking the whole graph at
/// every call or write site.
pub(crate) struct CandidateObjectIndex {
    objects: Vec<ValueOrigin>,
    reachable: BTreeMap<ValueOrigin, Vec<u64>>,
    bindings: BTreeMap<Id, Vec<u64>>,
}

impl CandidateObjectIndex {
    fn visit_bits(&self, bits: &[u64], mut visit: impl FnMut(ValueOrigin)) {
        for (word_index, word) in bits.iter().copied().enumerate() {
            let mut remaining = word;
            while remaining != 0 {
                let bit = remaining.trailing_zeros() as usize;
                visit(self.objects[word_index * 64 + bit]);
                remaining &= remaining - 1;
            }
        }
    }

    pub(crate) fn for_binding(&self, id: &Id, visit: impl FnMut(ValueOrigin)) {
        if let Some(bits) = self.bindings.get(id) {
            self.visit_bits(bits, visit);
        }
    }

    pub(crate) fn for_values(&self, values: &Values, mut visit: impl FnMut(ValueOrigin)) {
        for value in values {
            if let Some(bits) = self.reachable.get(value) {
                self.visit_bits(bits, &mut visit);
            }
        }
    }
}

fn union_bits(into: &mut [u64], from: &[u64]) -> bool {
    let mut changed = false;
    for (slot, bits) in into.iter_mut().zip(from) {
        let merged = *slot | bits;
        changed |= merged != *slot;
        *slot = merged;
    }
    changed
}

#[derive(Debug, Clone, Default)]
#[cfg_attr(test, derive(PartialEq, Eq))]
pub(crate) struct ChunkValueFlow {
    declared_ids: BTreeSet<Id>,
    bindings: BTreeMap<Id, Values>,
    properties: BTreeMap<(u32, String), Values>,
    returns: BTreeMap<u32, Values>,
    params: BTreeMap<u32, Vec<Option<Id>>>,
    /// Callable identity to its chunk-top declaration binding, if any.
    canonical: BTreeMap<ValueOrigin, Id>,
    pub(crate) callable_owner: BTreeMap<ValueOrigin, StatementOrdinal>,
}

impl ChunkValueFlow {
    pub(crate) fn candidate_object_index(
        &self,
        candidates: &BTreeSet<ValueOrigin>,
    ) -> CandidateObjectIndex {
        let objects: Vec<_> = candidates.iter().copied().collect();
        let words = objects.len().div_ceil(64);
        let mut reverse: BTreeMap<ValueOrigin, BTreeSet<ValueOrigin>> = BTreeMap::new();
        for (function, values) in &self.returns {
            for value in values {
                reverse
                    .entry(*value)
                    .or_default()
                    .insert(ValueOrigin::Function(*function));
            }
        }
        for ((object, _), values) in &self.properties {
            for value in values {
                reverse
                    .entry(*value)
                    .or_default()
                    .insert(ValueOrigin::Object(*object));
            }
        }
        let mut reachable = BTreeMap::new();
        let mut pending = VecDeque::new();
        let mut queued = BTreeSet::new();
        for (index, object) in objects.iter().copied().enumerate() {
            let bits = reachable.entry(object).or_insert_with(|| vec![0; words]);
            bits[index / 64] |= 1 << (index % 64);
            pending.push_back(object);
            queued.insert(object);
        }
        while let Some(value) = pending.pop_front() {
            queued.remove(&value);
            let bits = reachable[&value].clone();
            for predecessor in reverse.get(&value).into_iter().flatten() {
                let target = reachable
                    .entry(*predecessor)
                    .or_insert_with(|| vec![0; words]);
                if union_bits(target, &bits) && queued.insert(*predecessor) {
                    pending.push_back(*predecessor);
                }
            }
        }
        let bindings = self
            .bindings
            .iter()
            .filter_map(|(id, values)| {
                let mut bits = vec![0; words];
                for value in values {
                    if let Some(found) = reachable.get(value) {
                        union_bits(&mut bits, found);
                    }
                }
                bits.iter()
                    .any(|word| *word != 0)
                    .then_some((id.clone(), bits))
            })
            .collect();
        CandidateObjectIndex {
            objects,
            reachable,
            bindings,
        }
    }

    pub(crate) fn analyze(body: &[TopLevelItemView<'_>]) -> Self {
        FlowProgram::collect(body).solve()
    }

    #[cfg(test)]
    pub(crate) fn analyze_with_stats(body: &[TopLevelItemView<'_>]) -> (Self, FlowWorkStats) {
        let mut solver = FlowProgram::collect(body).into_solver();
        solver.run();
        (solver.flow, solver.stats)
    }

    #[cfg(test)]
    pub(crate) fn analyze_reference(body: &[TopLevelItemView<'_>]) -> Self {
        let mut flow = Self::default();
        let mut declarations = DeclaredBindingCollector::default();
        for item in body {
            item.as_module_item().visit_with(&mut declarations);
        }
        flow.declared_ids = declarations.ids;
        loop {
            let mut changed = false;
            for (ordinal, item) in body.iter().enumerate() {
                let mut pass = FlowPass {
                    flow: &mut flow,
                    changed: &mut changed,
                    owner: StatementOrdinal(ordinal),
                    current_function: None,
                };
                item.as_module_item().visit_with(&mut pass);
            }
            if !changed {
                break;
            }
        }
        flow
    }

    fn merge_binding(&mut self, id: Id, values: Values) -> bool {
        if values.is_empty() {
            return false;
        }
        let entry = self.bindings.entry(id).or_default();
        let before = entry.len();
        entry.extend(values);
        entry.len() != before
    }

    fn merge_property(&mut self, object: u32, prop: String, values: Values) -> bool {
        if values.is_empty() {
            return false;
        }
        let entry = self.properties.entry((object, prop)).or_default();
        let before = entry.len();
        entry.extend(values);
        entry.len() != before
    }

    fn merge_return(&mut self, function: u32, values: Values) -> bool {
        if values.is_empty() {
            return false;
        }
        let entry = self.returns.entry(function).or_default();
        let before = entry.len();
        entry.extend(values);
        entry.len() != before
    }

    pub(crate) fn values_of_binding(&self, id: &Id) -> Values {
        self.bindings.get(id).cloned().unwrap_or_else(|| {
            if self.declared_ids.contains(id) {
                Values::new()
            } else {
                unknown()
            }
        })
    }

    pub(crate) fn is_declared(&self, id: &Id) -> bool {
        self.declared_ids.contains(id)
    }

    pub(crate) fn values_of_expr(&self, expr: &Expr) -> Values {
        match expr.unwrap_parens() {
            Expr::Ident(ident) => self.values_of_binding(&ident.to_id()),
            Expr::Fn(function) => {
                BTreeSet::from([ValueOrigin::Function(function.function.span.lo.0)])
            }
            Expr::Arrow(arrow) => BTreeSet::from([ValueOrigin::Function(arrow.span.lo.0)]),
            Expr::Class(class) => BTreeSet::from([ValueOrigin::Class(class.class.span.lo.0)]),
            Expr::Object(object) => BTreeSet::from([ValueOrigin::Object(object.span.lo.0)]),
            Expr::Array(array) => BTreeSet::from([ValueOrigin::Object(array.span.lo.0)]),
            Expr::Member(member) => {
                let prop = static_prop(&member.prop);
                let mut out = Values::new();
                for origin in self.values_of_expr(&member.obj) {
                    if let ValueOrigin::Object(object) = origin {
                        if let Some(prop) = &prop {
                            out.extend(
                                self.properties
                                    .get(&(object, prop.clone()))
                                    .cloned()
                                    .unwrap_or_else(unknown),
                            );
                        } else {
                            out.extend(
                                self.properties
                                    .range((object, String::new())..)
                                    .take_while(|((owner, _), _)| *owner == object)
                                    .flat_map(|(_, values)| values.iter().copied()),
                            );
                            out.insert(ValueOrigin::Unknown);
                        }
                    } else {
                        out.insert(ValueOrigin::Unknown);
                    }
                }
                out
            }
            Expr::Call(call) => {
                let Callee::Expr(callee) = &call.callee else {
                    return unknown();
                };
                let mut out = Values::new();
                for origin in self.values_of_expr(callee) {
                    if let ValueOrigin::Function(function) = origin {
                        out.extend(self.returns.get(&function).cloned().unwrap_or_default());
                    } else {
                        out.insert(ValueOrigin::Unknown);
                    }
                }
                out
            }
            Expr::Cond(cond) => self
                .values_of_expr(&cond.cons)
                .union(&self.values_of_expr(&cond.alt))
                .copied()
                .collect(),
            Expr::Bin(bin)
                if matches!(
                    bin.op,
                    BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing
                ) =>
            {
                self.values_of_expr(&bin.left)
                    .union(&self.values_of_expr(&bin.right))
                    .copied()
                    .collect()
            }
            Expr::Seq(seq) => seq
                .exprs
                .last()
                .map_or_else(unknown, |last| self.values_of_expr(last)),
            Expr::Assign(assign) => self.values_of_expr(&assign.right),
            Expr::Lit(_) | Expr::This(_) | Expr::Tpl(_) => Values::new(),
            _ => unknown(),
        }
    }

    fn values_of_binding_tracking(
        &self,
        id: &Id,
        dependencies: &mut BTreeSet<FlowDependency>,
    ) -> Values {
        dependencies.insert(FlowDependency::Binding(id.clone()));
        self.values_of_binding(id)
    }

    fn values_of_value_expr_tracking(
        &self,
        expr: &FlowValueExpr,
        dependencies: &mut BTreeSet<FlowDependency>,
    ) -> Values {
        match expr {
            FlowValueExpr::Binding(id) => self.values_of_binding_tracking(id, dependencies),
            FlowValueExpr::Value(value) => BTreeSet::from([*value]),
            FlowValueExpr::Member { object, property } => {
                let mut values = Values::new();
                for origin in self.values_of_value_expr_tracking(object, dependencies) {
                    if let ValueOrigin::Object(object) = origin {
                        if let Some(property) = property {
                            // Keep watching absent properties. Their Unknown is durable, while a later
                            // write can add concrete values.
                            dependencies.insert(FlowDependency::Property(object, property.clone()));
                            values.extend(
                                self.properties
                                    .get(&(object, property.clone()))
                                    .cloned()
                                    .unwrap_or_else(unknown),
                            );
                        } else {
                            dependencies.insert(FlowDependency::Properties(object));
                            values.extend(
                                self.properties
                                    .range((object, String::new())..)
                                    .take_while(|((owner, _), _)| *owner == object)
                                    .flat_map(|(_, values)| values.iter().copied()),
                            );
                            values.insert(ValueOrigin::Unknown);
                        }
                    } else {
                        values.insert(ValueOrigin::Unknown);
                    }
                }
                values
            }
            FlowValueExpr::Call(callee) => {
                let mut values = Values::new();
                for origin in self.values_of_value_expr_tracking(callee, dependencies) {
                    if let ValueOrigin::Function(function) = origin {
                        dependencies.insert(FlowDependency::Return(function));
                        values.extend(self.returns.get(&function).cloned().unwrap_or_default());
                    } else {
                        values.insert(ValueOrigin::Unknown);
                    }
                }
                values
            }
            FlowValueExpr::Union(left, right) => self
                .values_of_value_expr_tracking(left, dependencies)
                .union(&self.values_of_value_expr_tracking(right, dependencies))
                .copied()
                .collect(),
            FlowValueExpr::Empty => Values::new(),
            FlowValueExpr::Unknown => unknown(),
        }
    }

    pub(crate) fn canonical_callable(&self, origin: ValueOrigin) -> Option<&Id> {
        self.canonical.get(&origin)
    }

    pub(crate) fn values_of_property(&self, object: u32, property: &str) -> Option<&Values> {
        self.properties.get(&(object, property.to_owned()))
    }

    pub(crate) fn aliases_by_object(
        &self,
        objects: &BTreeSet<ValueOrigin>,
    ) -> BTreeMap<ValueOrigin, BTreeSet<String>> {
        let mut aliases: BTreeMap<ValueOrigin, BTreeSet<String>> = BTreeMap::new();
        for (id, values) in &self.bindings {
            for object in values.intersection(objects) {
                aliases.entry(*object).or_default().insert(id.0.to_string());
            }
        }
        aliases
    }

    /// Add call targets discovered through parameters, aliases, returns and
    /// static object properties to the ordinary syntactic call buckets.
    pub(crate) fn augment_calls(
        &self,
        body: &[TopLevelItemView<'_>],
        facts: &mut [StatementFacts],
    ) {
        for (item, fact) in body.iter().zip(facts) {
            let mut collector = FlowCallCollector {
                flow: self,
                calls: PositionBucketed::default(),
                complete_ident_calls: PositionBucketed::default(),
                incomplete_ident_calls: PositionBucketed::default(),
                lazy_depth: 0,
                past_await: false,
                may_skip_await_depth: 0,
            };
            item.as_module_item().visit_with(&mut collector);
            for id in collector
                .complete_ident_calls
                .eager
                .difference(&collector.incomplete_ident_calls.eager)
            {
                fact.calls.eager.remove(id);
            }
            for id in collector
                .complete_ident_calls
                .lazy
                .difference(&collector.incomplete_ident_calls.lazy)
            {
                fact.calls.lazy.remove(id);
            }
            for id in collector
                .complete_ident_calls
                .first_order_lazy
                .difference(&collector.incomplete_ident_calls.first_order_lazy)
            {
                fact.calls.first_order_lazy.remove(id);
            }
            fact.calls.extend(collector.calls);
            if let (Some(class), Some(constructor)) = (
                super::class_of_item(item.as_module_item()),
                fact.constructor.as_mut(),
            ) {
                let mut ctor_calls = FlowCallCollector {
                    flow: self,
                    calls: PositionBucketed::default(),
                    complete_ident_calls: PositionBucketed::default(),
                    incomplete_ident_calls: PositionBucketed::default(),
                    lazy_depth: 0,
                    past_await: false,
                    may_skip_await_depth: 0,
                };
                for member in &class.body {
                    if matches!(member, ClassMember::Constructor(_))
                        || matches!(member, ClassMember::ClassProp(prop) if !prop.is_static)
                        || matches!(member, ClassMember::PrivateProp(prop) if !prop.is_static)
                        || matches!(member, ClassMember::AutoAccessor(accessor) if !accessor.is_static)
                    {
                        member.visit_with(&mut ctor_calls);
                    }
                }
                for id in ctor_calls
                    .complete_ident_calls
                    .first_order_lazy
                    .difference(&ctor_calls.incomplete_ident_calls.first_order_lazy)
                {
                    constructor.calls.remove(id);
                }
                constructor.calls.extend(ctor_calls.calls.first_order_lazy);
            }
        }
    }
}

#[derive(Default)]
#[cfg(test)]
struct DeclaredBindingCollector {
    ids: BTreeSet<Id>,
}

#[cfg(test)]
impl Visit for DeclaredBindingCollector {
    fn visit_binding_ident(&mut self, node: &BindingIdent) {
        self.ids.insert(node.id.to_id());
    }

    fn visit_fn_decl(&mut self, node: &FnDecl) {
        self.ids.insert(node.ident.to_id());
        node.function.visit_with(self);
    }

    fn visit_class_decl(&mut self, node: &ClassDecl) {
        self.ids.insert(node.ident.to_id());
        node.class.visit_with(self);
    }
}

#[derive(Debug, Clone)]
enum FlowValueExpr {
    Binding(Id),
    Value(ValueOrigin),
    Member {
        object: Box<FlowValueExpr>,
        property: Option<String>,
    },
    Call(Box<FlowValueExpr>),
    Union(Box<FlowValueExpr>, Box<FlowValueExpr>),
    Empty,
    Unknown,
}

impl FlowValueExpr {
    fn from_ast(expr: &Expr) -> Self {
        match expr.unwrap_parens() {
            Expr::Ident(ident) => Self::Binding(ident.to_id()),
            Expr::Fn(function) => Self::Value(ValueOrigin::Function(function.function.span.lo.0)),
            Expr::Arrow(arrow) => Self::Value(ValueOrigin::Function(arrow.span.lo.0)),
            Expr::Class(class) => Self::Value(ValueOrigin::Class(class.class.span.lo.0)),
            Expr::Object(object) => Self::Value(ValueOrigin::Object(object.span.lo.0)),
            Expr::Array(array) => Self::Value(ValueOrigin::Object(array.span.lo.0)),
            Expr::Member(member) => Self::Member {
                object: Box::new(Self::from_ast(&member.obj)),
                property: static_prop(&member.prop),
            },
            Expr::Call(call) => match &call.callee {
                Callee::Expr(callee) => Self::Call(Box::new(Self::from_ast(callee))),
                _ => Self::Unknown,
            },
            Expr::Cond(cond) => Self::Union(
                Box::new(Self::from_ast(&cond.cons)),
                Box::new(Self::from_ast(&cond.alt)),
            ),
            Expr::Bin(bin)
                if matches!(
                    bin.op,
                    BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing
                ) =>
            {
                Self::Union(
                    Box::new(Self::from_ast(&bin.left)),
                    Box::new(Self::from_ast(&bin.right)),
                )
            }
            Expr::Seq(seq) => seq
                .exprs
                .last()
                .map_or(Self::Unknown, |last| Self::from_ast(last)),
            Expr::Assign(assign) => Self::from_ast(&assign.right),
            Expr::Lit(_) | Expr::This(_) | Expr::Tpl(_) => Self::Empty,
            _ => Self::Unknown,
        }
    }
}

#[derive(Debug, Clone)]
enum FlowEvent {
    FunctionDeclaration {
        id: Id,
        origin: ValueOrigin,
    },
    ClassDeclaration {
        id: Id,
        origin: ValueOrigin,
        owner: StatementOrdinal,
    },
    Variable {
        id: Id,
        init: FlowValueExpr,
        canonical: Option<ValueOrigin>,
    },
    AssignBinding {
        id: Id,
        right: FlowValueExpr,
    },
    AssignProperty {
        object: FlowValueExpr,
        property: String,
        right: FlowValueExpr,
    },
    WritePropertyExpr {
        object: u32,
        property: String,
        value: FlowValueExpr,
    },
    WritePropertyBinding {
        object: u32,
        property: String,
        binding: Id,
    },
    WritePropertyValue {
        object: u32,
        property: String,
        value: ValueOrigin,
    },
    FunctionMetadata {
        function: u32,
        params: Vec<Option<Id>>,
        owner: StatementOrdinal,
    },
    Return {
        function: u32,
        value: FlowValueExpr,
    },
    Invoke {
        callee: FlowValueExpr,
        args: Vec<FlowValueExpr>,
    },
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
enum FlowDependency {
    Binding(Id),
    Property(u32, String),
    Properties(u32),
    Return(u32),
    Parameters(u32),
}

struct FlowProgram {
    declared_ids: BTreeSet<Id>,
    events: Vec<FlowEvent>,
}

impl FlowProgram {
    fn collect(body: &[TopLevelItemView<'_>]) -> Self {
        let mut collector = FlowProgramCollector {
            declared_ids: BTreeSet::new(),
            events: Vec::new(),
            owner: StatementOrdinal(0),
            current_function: None,
        };
        for (ordinal, item) in body.iter().enumerate() {
            collector.owner = StatementOrdinal(ordinal);
            item.as_module_item().visit_with(&mut collector);
        }
        Self {
            declared_ids: collector.declared_ids,
            events: collector.events,
        }
    }

    fn into_solver(self) -> FlowSolver {
        FlowSolver {
            flow: ChunkValueFlow {
                declared_ids: self.declared_ids,
                ..ChunkValueFlow::default()
            },
            events: self.events,
            dependencies: Vec::new(),
            watchers: BTreeMap::new(),
            pending: BTreeSet::new(),
            scheduled_round: Vec::new(),
            round_zero_new_parameters: BTreeSet::new(),
            round_zero_had_fact_change: false,
            #[cfg(test)]
            stats: FlowWorkStats::default(),
        }
        .with_event_slots()
    }

    fn solve(self) -> ChunkValueFlow {
        let mut solver = self.into_solver();
        solver.run();
        solver.flow
    }
}

struct FlowProgramCollector {
    declared_ids: BTreeSet<Id>,
    events: Vec<FlowEvent>,
    owner: StatementOrdinal,
    current_function: Option<u32>,
}

impl Visit for FlowProgramCollector {
    fn visit_binding_ident(&mut self, node: &BindingIdent) {
        self.declared_ids.insert(node.id.to_id());
    }

    fn visit_fn_decl(&mut self, node: &FnDecl) {
        let id = node.ident.to_id();
        self.declared_ids.insert(id.clone());
        self.events.push(FlowEvent::FunctionDeclaration {
            id,
            origin: ValueOrigin::Function(node.function.span.lo.0),
        });
        node.function.visit_with(self);
    }

    fn visit_class_decl(&mut self, node: &ClassDecl) {
        let id = node.ident.to_id();
        self.declared_ids.insert(id.clone());
        self.events.push(FlowEvent::ClassDeclaration {
            id,
            origin: ValueOrigin::Class(node.class.span.lo.0),
            owner: self.owner,
        });
        node.class.visit_with(self);
    }

    fn visit_var_declarator(&mut self, node: &VarDeclarator) {
        if let (Pat::Ident(binding), Some(init)) = (&node.name, node.init.as_deref()) {
            let canonical = self
                .current_function
                .is_none()
                .then(|| direct_callable_origin(init))
                .flatten();
            self.events.push(FlowEvent::Variable {
                id: binding.id.to_id(),
                init: FlowValueExpr::from_ast(init),
                canonical,
            });
        }
        node.visit_children_with(self);
    }

    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        match &node.left {
            AssignTarget::Simple(SimpleAssignTarget::Ident(binding)) => {
                self.events.push(FlowEvent::AssignBinding {
                    id: binding.id.to_id(),
                    right: FlowValueExpr::from_ast(&node.right),
                });
            }
            AssignTarget::Simple(SimpleAssignTarget::Member(member)) => {
                if let Some(property) = static_prop(&member.prop) {
                    self.events.push(FlowEvent::AssignProperty {
                        object: FlowValueExpr::from_ast(&member.obj),
                        property,
                        right: FlowValueExpr::from_ast(&node.right),
                    });
                }
            }
            _ => {}
        }
        node.visit_children_with(self);
    }

    fn visit_object_lit(&mut self, node: &ObjectLit) {
        let object = node.span.lo.0;
        for prop in &node.props {
            if let PropOrSpread::Prop(prop) = prop {
                match prop.as_ref() {
                    Prop::KeyValue(kv) => {
                        if let Some(property) = object_prop(&kv.key) {
                            self.events.push(FlowEvent::WritePropertyExpr {
                                object,
                                property,
                                value: FlowValueExpr::from_ast(&kv.value),
                            });
                        }
                    }
                    Prop::Shorthand(ident) => {
                        self.events.push(FlowEvent::WritePropertyBinding {
                            object,
                            property: ident.sym.to_string(),
                            binding: ident.to_id(),
                        });
                    }
                    Prop::Method(method) => {
                        if let Some(property) = object_prop(&method.key) {
                            self.events.push(FlowEvent::WritePropertyValue {
                                object,
                                property,
                                value: ValueOrigin::Function(method.function.span.lo.0),
                            });
                        }
                    }
                    _ => {}
                }
            }
        }
        node.visit_children_with(self);
    }

    fn visit_array_lit(&mut self, node: &ArrayLit) {
        let object = node.span.lo.0;
        for (index, element) in node.elems.iter().enumerate() {
            if let Some(element) = element {
                self.events.push(FlowEvent::WritePropertyExpr {
                    object,
                    property: index.to_string(),
                    value: FlowValueExpr::from_ast(&element.expr),
                });
            }
        }
        node.visit_children_with(self);
    }

    fn visit_function(&mut self, node: &Function) {
        let function = node.span.lo.0;
        self.events.push(FlowEvent::FunctionMetadata {
            function,
            params: node
                .params
                .iter()
                .map(|param| match &param.pat {
                    Pat::Ident(binding) => Some(binding.id.to_id()),
                    _ => None,
                })
                .collect(),
            owner: self.owner,
        });
        let previous = self.current_function.replace(function);
        node.visit_children_with(self);
        self.current_function = previous;
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        let function = node.span.lo.0;
        self.events.push(FlowEvent::FunctionMetadata {
            function,
            params: node
                .params
                .iter()
                .map(|param| match param {
                    Pat::Ident(binding) => Some(binding.id.to_id()),
                    _ => None,
                })
                .collect(),
            owner: self.owner,
        });
        let previous = self.current_function.replace(function);
        if let ArrowFunctionBody::Expr(expr) = node.body.as_ref() {
            self.events.push(FlowEvent::Return {
                function,
                value: FlowValueExpr::from_ast(expr),
            });
        }
        node.visit_children_with(self);
        self.current_function = previous;
    }

    fn visit_return_stmt(&mut self, node: &ReturnStmt) {
        if let (Some(function), Some(value)) = (self.current_function, node.arg.as_deref()) {
            self.events.push(FlowEvent::Return {
                function,
                value: FlowValueExpr::from_ast(value),
            });
        }
        node.visit_children_with(self);
    }

    fn visit_call_expr(&mut self, node: &CallExpr) {
        if let Callee::Expr(callee) = &node.callee {
            self.events.push(FlowEvent::Invoke {
                callee: FlowValueExpr::from_ast(callee),
                args: node
                    .args
                    .iter()
                    .map(|arg| FlowValueExpr::from_ast(&arg.expr))
                    .collect(),
            });
        }
        node.visit_children_with(self);
    }

    fn visit_new_expr(&mut self, node: &NewExpr) {
        if let Some(args) = &node.args {
            self.events.push(FlowEvent::Invoke {
                callee: FlowValueExpr::from_ast(&node.callee),
                args: args
                    .iter()
                    .map(|arg| FlowValueExpr::from_ast(&arg.expr))
                    .collect(),
            });
        }
        node.visit_children_with(self);
    }
}

fn direct_callable_origin(expr: &Expr) -> Option<ValueOrigin> {
    match expr.unwrap_parens() {
        Expr::Fn(function) => Some(ValueOrigin::Function(function.function.span.lo.0)),
        Expr::Arrow(arrow) => Some(ValueOrigin::Function(arrow.span.lo.0)),
        Expr::Class(class) => Some(ValueOrigin::Class(class.class.span.lo.0)),
        _ => None,
    }
}

#[cfg(test)]
#[derive(Debug, Default, Clone, Copy, Eq, PartialEq)]
pub(crate) struct FlowWorkStats {
    pub(crate) event_executions: usize,
}

struct FlowSolver {
    flow: ChunkValueFlow,
    events: Vec<FlowEvent>,
    dependencies: Vec<BTreeSet<FlowDependency>>,
    watchers: BTreeMap<FlowDependency, BTreeSet<usize>>,
    pending: BTreeSet<(usize, usize)>,
    scheduled_round: Vec<Option<usize>>,
    round_zero_new_parameters: BTreeSet<u32>,
    round_zero_had_fact_change: bool,
    #[cfg(test)]
    stats: FlowWorkStats,
}

impl FlowSolver {
    fn with_event_slots(mut self) -> Self {
        self.dependencies = vec![BTreeSet::new(); self.events.len()];
        self.scheduled_round = vec![None; self.events.len()];
        self
    }

    fn run(&mut self) {
        for index in 0..self.events.len() {
            self.run_event(index, 0, true);
        }
        // Legacy convergence only counted value-map merges, not params metadata.
        if self.round_zero_had_fact_change {
            let functions = std::mem::take(&mut self.round_zero_new_parameters);
            for function in functions {
                if let Some(watchers) = self.watchers.get(&FlowDependency::Parameters(function)) {
                    let watchers: Vec<_> = watchers.iter().copied().collect();
                    for watcher in watchers {
                        self.schedule(watcher, 1);
                    }
                }
            }
        }
        while let Some((round, index)) = self.pending.pop_first() {
            self.scheduled_round[index] = None;
            self.run_event(index, round, false);
        }
    }

    fn run_event(&mut self, index: usize, round: usize, initial_sweep: bool) {
        #[cfg(test)]
        {
            self.stats.event_executions += 1;
        }

        let event = self.events[index].clone();
        let mut dependencies = BTreeSet::new();
        let mut changed = BTreeSet::new();
        match event {
            FlowEvent::FunctionDeclaration { id, origin } => {
                self.flow
                    .canonical
                    .entry(origin)
                    .or_insert_with(|| id.clone());
                self.merge_binding(id, BTreeSet::from([origin]), &mut changed);
            }
            FlowEvent::ClassDeclaration { id, origin, owner } => {
                self.flow
                    .canonical
                    .entry(origin)
                    .or_insert_with(|| id.clone());
                self.flow.callable_owner.entry(origin).or_insert(owner);
                self.merge_binding(id, BTreeSet::from([origin]), &mut changed);
            }
            FlowEvent::Variable {
                id,
                init,
                canonical,
            } => {
                let values = self
                    .flow
                    .values_of_value_expr_tracking(&init, &mut dependencies);
                if let Some(origin) = canonical
                    && values.contains(&origin)
                {
                    self.flow
                        .canonical
                        .entry(origin)
                        .or_insert_with(|| id.clone());
                }
                self.merge_binding(id, values, &mut changed);
            }
            FlowEvent::AssignBinding { id, right } => {
                let values = self
                    .flow
                    .values_of_value_expr_tracking(&right, &mut dependencies);
                self.merge_binding(id, values, &mut changed);
            }
            FlowEvent::AssignProperty {
                object,
                property,
                right,
            } => {
                let values = self
                    .flow
                    .values_of_value_expr_tracking(&right, &mut dependencies);
                let objects = self
                    .flow
                    .values_of_value_expr_tracking(&object, &mut dependencies);
                for origin in objects {
                    if let ValueOrigin::Object(object) = origin {
                        self.merge_property(object, property.clone(), values.clone(), &mut changed);
                    }
                }
            }
            FlowEvent::WritePropertyExpr {
                object,
                property,
                value,
            } => {
                let values = self
                    .flow
                    .values_of_value_expr_tracking(&value, &mut dependencies);
                self.merge_property(object, property, values, &mut changed);
            }
            FlowEvent::WritePropertyBinding {
                object,
                property,
                binding,
            } => {
                let values = self
                    .flow
                    .values_of_binding_tracking(&binding, &mut dependencies);
                self.merge_property(object, property, values, &mut changed);
            }
            FlowEvent::WritePropertyValue {
                object,
                property,
                value,
            } => {
                self.merge_property(object, property, BTreeSet::from([value]), &mut changed);
            }
            FlowEvent::FunctionMetadata {
                function,
                params,
                owner,
            } => {
                if let std::collections::btree_map::Entry::Vacant(entry) =
                    self.flow.params.entry(function)
                {
                    entry.insert(params);
                    self.round_zero_new_parameters.insert(function);
                }
                self.flow
                    .callable_owner
                    .entry(ValueOrigin::Function(function))
                    .or_insert(owner);
            }
            FlowEvent::Return { function, value } => {
                let values = self
                    .flow
                    .values_of_value_expr_tracking(&value, &mut dependencies);
                self.merge_return(function, values, &mut changed);
            }
            FlowEvent::Invoke { callee, args } => {
                let targets = self
                    .flow
                    .values_of_value_expr_tracking(&callee, &mut dependencies);
                for target in targets {
                    let ValueOrigin::Function(function) = target else {
                        continue;
                    };
                    let Some(params) = self.flow.params.get(&function).cloned() else {
                        dependencies.insert(FlowDependency::Parameters(function));
                        continue;
                    };
                    for (param, arg) in params.into_iter().zip(&args) {
                        if let Some(param) = param {
                            let values = self
                                .flow
                                .values_of_value_expr_tracking(arg, &mut dependencies);
                            self.merge_binding(param, values, &mut changed);
                        }
                    }
                }
            }
        }
        if initial_sweep && !changed.is_empty() {
            self.round_zero_had_fact_change = true;
        }
        self.record_event_dependencies(index, dependencies);
        self.schedule_changed_events(index, round, initial_sweep, changed);
    }

    fn merge_binding(&mut self, id: Id, values: Values, changed: &mut BTreeSet<FlowDependency>) {
        if self.flow.merge_binding(id.clone(), values) {
            changed.insert(FlowDependency::Binding(id));
        }
    }

    fn merge_property(
        &mut self,
        object: u32,
        property: String,
        values: Values,
        changed: &mut BTreeSet<FlowDependency>,
    ) {
        if self.flow.merge_property(object, property.clone(), values) {
            changed.insert(FlowDependency::Property(object, property));
            changed.insert(FlowDependency::Properties(object));
        }
    }

    fn merge_return(
        &mut self,
        function: u32,
        values: Values,
        changed: &mut BTreeSet<FlowDependency>,
    ) {
        if self.flow.merge_return(function, values) {
            changed.insert(FlowDependency::Return(function));
        }
    }

    fn record_event_dependencies(&mut self, index: usize, dependencies: BTreeSet<FlowDependency>) {
        for dependency in dependencies {
            if self.dependencies[index].insert(dependency.clone()) {
                self.watchers.entry(dependency).or_default().insert(index);
            }
        }
    }

    fn schedule_changed_events(
        &mut self,
        index: usize,
        round: usize,
        initial_sweep: bool,
        changed: BTreeSet<FlowDependency>,
    ) {
        // Preserve sweep timing: only later source-order events can run this round.
        for dependency in changed {
            let Some(watchers) = self.watchers.get(&dependency) else {
                continue;
            };
            let watchers: Vec<_> = watchers.iter().copied().collect();
            for watcher in watchers {
                if initial_sweep && watcher > index {
                    continue;
                }
                let next_round = if watcher > index {
                    round
                } else {
                    round.checked_add(1).expect("value-flow round overflow")
                };
                self.schedule(watcher, next_round);
            }
        }
    }

    fn schedule(&mut self, index: usize, round: usize) {
        if self.scheduled_round[index].is_some_and(|scheduled| scheduled <= round) {
            return;
        }
        if let Some(scheduled) = self.scheduled_round[index].replace(round) {
            self.pending.remove(&(scheduled, index));
        }
        self.pending.insert((round, index));
    }
}

fn unknown() -> Values {
    BTreeSet::from([ValueOrigin::Unknown])
}

pub(crate) fn static_prop(prop: &MemberProp) -> Option<String> {
    match prop {
        MemberProp::Ident(ident) => Some(ident.sym.to_string()),
        MemberProp::Computed(computed) => match computed.expr.unwrap_parens() {
            Expr::Lit(Lit::Str(string)) => Some(string.value.to_string_lossy().into_owned()),
            _ => None,
        },
        MemberProp::PrivateName(_) => None,
    }
}

fn object_prop(prop: &PropName) -> Option<String> {
    match prop {
        PropName::Ident(ident) => Some(ident.sym.to_string()),
        PropName::Str(string) => Some(string.value.to_string_lossy().into_owned()),
        PropName::Num(num) => Some(num.value.to_string()),
        PropName::Computed(_) | PropName::BigInt(_) => None,
    }
}

#[cfg(test)]
struct FlowPass<'a> {
    flow: &'a mut ChunkValueFlow,
    changed: &'a mut bool,
    owner: StatementOrdinal,
    current_function: Option<u32>,
}

#[cfg(test)]
impl FlowPass<'_> {
    fn merge_binding(&mut self, id: Id, values: Values) {
        *self.changed |= self.flow.merge_binding(id, values);
    }

    fn invoke(&mut self, callee: &Expr, args: &[ExprOrSpread]) {
        let targets = self.flow.values_of_expr(callee);
        for target in targets {
            let ValueOrigin::Function(function) = target else {
                continue;
            };
            let Some(params) = self.flow.params.get(&function).cloned() else {
                continue;
            };
            for (param, arg) in params.into_iter().zip(args) {
                if let Some(param) = param {
                    let values = self.flow.values_of_expr(&arg.expr);
                    self.merge_binding(param, values);
                }
            }
        }
    }

    fn visit_function_body(&mut self, function: u32, visit: impl FnOnce(&mut Self)) {
        self.flow
            .callable_owner
            .entry(ValueOrigin::Function(function))
            .or_insert(self.owner);
        let previous = self.current_function.replace(function);
        visit(self);
        self.current_function = previous;
    }
}

#[cfg(test)]
impl Visit for FlowPass<'_> {
    fn visit_fn_decl(&mut self, node: &FnDecl) {
        let key = node.function.span.lo.0;
        let value = ValueOrigin::Function(key);
        self.flow
            .canonical
            .entry(value)
            .or_insert_with(|| node.ident.to_id());
        self.merge_binding(node.ident.to_id(), BTreeSet::from([value]));
        node.function.visit_with(self);
    }

    fn visit_class_decl(&mut self, node: &ClassDecl) {
        let value = ValueOrigin::Class(node.class.span.lo.0);
        self.flow
            .canonical
            .entry(value)
            .or_insert_with(|| node.ident.to_id());
        self.flow.callable_owner.entry(value).or_insert(self.owner);
        self.merge_binding(node.ident.to_id(), BTreeSet::from([value]));
        node.class.visit_with(self);
    }

    fn visit_var_declarator(&mut self, node: &VarDeclarator) {
        if let Pat::Ident(binding) = &node.name
            && let Some(init) = node.init.as_deref()
        {
            let values = self.flow.values_of_expr(init);
            if self.current_function.is_none() {
                for value in &values {
                    if matches!(value, ValueOrigin::Function(_) | ValueOrigin::Class(_))
                        && matches!(
                            init.unwrap_parens(),
                            Expr::Fn(_) | Expr::Arrow(_) | Expr::Class(_)
                        )
                    {
                        self.flow
                            .canonical
                            .entry(*value)
                            .or_insert_with(|| binding.id.to_id());
                    }
                }
            }
            self.merge_binding(binding.id.to_id(), values);
        }
        node.visit_children_with(self);
    }

    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        let values = self.flow.values_of_expr(&node.right);
        match &node.left {
            AssignTarget::Simple(SimpleAssignTarget::Ident(binding)) => {
                self.merge_binding(binding.id.to_id(), values);
            }
            AssignTarget::Simple(SimpleAssignTarget::Member(member)) => {
                if let Some(prop) = static_prop(&member.prop) {
                    for origin in self.flow.values_of_expr(&member.obj) {
                        if let ValueOrigin::Object(object) = origin {
                            *self.changed |=
                                self.flow
                                    .merge_property(object, prop.clone(), values.clone());
                        }
                    }
                }
            }
            _ => {}
        }
        node.visit_children_with(self);
    }

    fn visit_object_lit(&mut self, node: &ObjectLit) {
        let object = node.span.lo.0;
        for prop in &node.props {
            if let PropOrSpread::Prop(prop) = prop {
                match prop.as_ref() {
                    Prop::KeyValue(kv) => {
                        if let Some(name) = object_prop(&kv.key) {
                            let values = self.flow.values_of_expr(&kv.value);
                            *self.changed |= self.flow.merge_property(object, name, values);
                        }
                    }
                    Prop::Shorthand(ident) => {
                        let values = self.flow.values_of_binding(&ident.to_id());
                        *self.changed |=
                            self.flow
                                .merge_property(object, ident.sym.to_string(), values);
                    }
                    Prop::Method(method) => {
                        if let Some(name) = object_prop(&method.key) {
                            *self.changed |= self.flow.merge_property(
                                object,
                                name,
                                BTreeSet::from([ValueOrigin::Function(method.function.span.lo.0)]),
                            );
                        }
                    }
                    _ => {}
                }
            }
        }
        node.visit_children_with(self);
    }

    fn visit_array_lit(&mut self, node: &ArrayLit) {
        let object = node.span.lo.0;
        for (index, element) in node.elems.iter().enumerate() {
            if let Some(element) = element {
                let values = self.flow.values_of_expr(&element.expr);
                *self.changed |= self.flow.merge_property(object, index.to_string(), values);
            }
        }
        node.visit_children_with(self);
    }

    fn visit_function(&mut self, node: &Function) {
        let key = node.span.lo.0;
        self.flow.params.entry(key).or_insert_with(|| {
            node.params
                .iter()
                .map(|param| match &param.pat {
                    Pat::Ident(binding) => Some(binding.id.to_id()),
                    _ => None,
                })
                .collect()
        });
        self.visit_function_body(key, |s| node.visit_children_with(s));
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        let key = node.span.lo.0;
        self.flow.params.entry(key).or_insert_with(|| {
            node.params
                .iter()
                .map(|param| match param {
                    Pat::Ident(binding) => Some(binding.id.to_id()),
                    _ => None,
                })
                .collect()
        });
        self.visit_function_body(key, |s| {
            if let ArrowFunctionBody::Expr(expr) = node.body.as_ref() {
                let values = s.flow.values_of_expr(expr);
                *s.changed |= s.flow.merge_return(key, values);
            }
            node.visit_children_with(s);
        });
    }

    fn visit_return_stmt(&mut self, node: &ReturnStmt) {
        if let (Some(function), Some(arg)) = (self.current_function, node.arg.as_deref()) {
            let values = self.flow.values_of_expr(arg);
            *self.changed |= self.flow.merge_return(function, values);
        }
        node.visit_children_with(self);
    }

    fn visit_call_expr(&mut self, node: &CallExpr) {
        if let Callee::Expr(callee) = &node.callee {
            self.invoke(callee, &node.args);
        }
        node.visit_children_with(self);
    }

    fn visit_new_expr(&mut self, node: &NewExpr) {
        if let Some(args) = &node.args {
            self.invoke(&node.callee, args);
        }
        node.visit_children_with(self);
    }
}

struct FlowCallCollector<'a> {
    flow: &'a ChunkValueFlow,
    calls: PositionBucketed<BTreeSet<Id>>,
    complete_ident_calls: PositionBucketed<BTreeSet<Id>>,
    incomplete_ident_calls: PositionBucketed<BTreeSet<Id>>,
    lazy_depth: u32,
    past_await: bool,
    may_skip_await_depth: u32,
}

impl LazyBoundary for FlowCallCollector<'_> {
    fn lazy_depth_mut(&mut self) -> &mut u32 {
        &mut self.lazy_depth
    }

    fn past_await_mut(&mut self) -> &mut bool {
        &mut self.past_await
    }
    fn may_skip_await_depth_mut(&mut self) -> Option<&mut u32> {
        Some(&mut self.may_skip_await_depth)
    }
}

impl FlowCallCollector<'_> {
    fn record(&mut self, callee: &Expr) {
        let values = self.flow.values_of_expr(callee);
        if let Expr::Ident(ident) = callee.unwrap_parens() {
            let id = ident.to_id();
            let bucket = if !values.is_empty()
                && values
                    .iter()
                    .all(|origin| self.flow.canonical_callable(*origin).is_some())
            {
                &mut self.complete_ident_calls
            } else {
                &mut self.incomplete_ident_calls
            };
            bucket.record(&id, self.lazy_depth, self.past_await);
        }
        for origin in values {
            if let Some(id) = self.flow.canonical_callable(origin) {
                self.calls.record(id, self.lazy_depth, self.past_await);
            }
        }
    }
}

impl Visit for FlowCallCollector<'_> {
    fn visit_opt_chain_expr(&mut self, node: &OptChainExpr) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        if matches!(
            node.op,
            AssignOp::AndAssign | AssignOp::OrAssign | AssignOp::NullishAssign
        ) {
            lazy_visit_may_skip(self, node);
        } else {
            node.visit_children_with(self);
        }
    }
    fn visit_if_stmt(&mut self, node: &IfStmt) {
        lazy_visit_if_stmt(self, node);
    }
    fn visit_cond_expr(&mut self, node: &CondExpr) {
        lazy_visit_cond_expr(self, node);
    }
    fn visit_bin_expr(&mut self, node: &BinExpr) {
        lazy_visit_bin_expr(self, node);
    }
    fn visit_while_stmt(&mut self, node: &WhileStmt) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_do_while_stmt(&mut self, node: &DoWhileStmt) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_for_stmt(&mut self, node: &ForStmt) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_for_in_stmt(&mut self, node: &ForInStmt) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_try_stmt(&mut self, node: &TryStmt) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_switch_stmt(&mut self, node: &SwitchStmt) {
        lazy_visit_may_skip(self, node);
    }

    fn visit_call_expr(&mut self, node: &CallExpr) {
        if let Callee::Expr(callee) = &node.callee {
            self.record(callee);
        }
        node.visit_children_with(self);
    }

    fn visit_new_expr(&mut self, node: &NewExpr) {
        self.record(&node.callee);
        node.visit_children_with(self);
    }

    fn visit_function(&mut self, node: &Function) {
        lazy_visit_function(self, node);
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        lazy_visit_arrow_expr(self, node);
    }

    fn visit_method_prop(&mut self, node: &MethodProp) {
        lazy_visit_method_prop(self, node);
    }

    fn visit_getter_prop(&mut self, node: &GetterProp) {
        lazy_visit_getter_prop(self, node);
    }

    fn visit_setter_prop(&mut self, node: &SetterProp) {
        lazy_visit_setter_prop(self, node);
    }

    fn visit_class(&mut self, node: &Class) {
        lazy_visit_class(self, node);
    }

    fn visit_class_member(&mut self, node: &ClassMember) {
        lazy_visit_class_member(self, node);
    }

    fn visit_await_expr(&mut self, node: &AwaitExpr) {
        node.arg.visit_with(self);
        if self.lazy_depth > 0 && self.may_skip_await_depth == 0 {
            self.past_await = true;
        }
    }
}
