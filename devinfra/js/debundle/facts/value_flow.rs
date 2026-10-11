//! Monotone, intra-chunk value identities for local calls and object aliases.
//!
//! The sets deliberately over-approximate: an unsupported expression yields
//! `Unknown`, and every assignment adds possible values rather than replacing
//! them. The same facts feed at-init call promotion and PlainData's escape
//! check, so an alias/return cannot be understood by one pass and invisible to
//! the other. This is context-insensitive; it makes no claim about values from
//! other chunks.

use std::collections::{BTreeMap, BTreeSet};

use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

use super::{
    LazyBoundary, PositionBucketed, StatementFacts, TopLevelItemView, lazy_visit_arrow_expr,
    lazy_visit_class, lazy_visit_class_member, lazy_visit_function, lazy_visit_getter_prop,
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

#[derive(Debug, Clone, Default)]
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
    pub(crate) fn analyze(body: &[TopLevelItemView<'_>]) -> Self {
        let mut flow = Self::default();
        let mut declarations = DeclaredBindingCollector::default();
        for item in body {
            item.as_module_item().visit_with(&mut declarations);
        }
        flow.declared_ids = declarations.ids;
        // Every iteration only adds finite value identities. A full pass is
        // required because returns and parameter values can travel backward
        // across source order and through recursive functions.
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
                                    .iter()
                                    .filter(|((owner, _), _)| *owner == object)
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
            Expr::Seq(seq) => seq
                .exprs
                .last()
                .map_or_else(unknown, |last| self.values_of_expr(last)),
            Expr::Assign(assign) => self.values_of_expr(&assign.right),
            Expr::Lit(_) | Expr::This(_) | Expr::Tpl(_) => Values::new(),
            _ => unknown(),
        }
    }

    pub(crate) fn canonical_callable(&self, origin: ValueOrigin) -> Option<&Id> {
        self.canonical.get(&origin)
    }

    pub(crate) fn return_values(&self, function: u32) -> Option<&Values> {
        self.returns.get(&function)
    }

    pub(crate) fn aliases_of_objects(&self, objects: &BTreeSet<ValueOrigin>) -> BTreeSet<String> {
        self.bindings
            .iter()
            .filter(|(_, values)| !values.is_disjoint(objects))
            .map(|(id, _)| id.0.to_string())
            .collect()
    }

    /// Values can themselves be functions which return the object. This is
    /// used only at escape sites, so an unknown value is left to the caller's
    /// conservative handling rather than being treated as a known object.
    pub(crate) fn may_reach_object(
        &self,
        values: &Values,
        objects: &BTreeSet<ValueOrigin>,
    ) -> bool {
        let mut pending: Vec<ValueOrigin> = values.iter().copied().collect();
        let mut seen = BTreeSet::new();
        while let Some(value) = pending.pop() {
            if !seen.insert(value) {
                continue;
            }
            if objects.contains(&value) {
                return true;
            }
            if let ValueOrigin::Function(function) = value
                && let Some(returns) = self.return_values(function)
            {
                pending.extend(returns.iter().copied());
            }
            if let ValueOrigin::Object(object) = value {
                pending.extend(
                    self.properties
                        .iter()
                        .filter(|((owner, _), _)| *owner == object)
                        .flat_map(|(_, values)| values.iter().copied()),
                );
            }
        }
        false
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
struct DeclaredBindingCollector {
    ids: BTreeSet<Id>,
}

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

fn unknown() -> Values {
    BTreeSet::from([ValueOrigin::Unknown])
}

fn static_prop(prop: &MemberProp) -> Option<String> {
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

struct FlowPass<'a> {
    flow: &'a mut ChunkValueFlow,
    changed: &'a mut bool,
    owner: StatementOrdinal,
    current_function: Option<u32>,
}

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
}

impl LazyBoundary for FlowCallCollector<'_> {
    fn lazy_depth_mut(&mut self) -> &mut u32 {
        &mut self.lazy_depth
    }

    fn past_await_mut(&mut self) -> &mut bool {
        &mut self.past_await
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
        if self.lazy_depth > 0 {
            self.past_await = true;
        }
    }
}
