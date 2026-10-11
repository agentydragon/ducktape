//! Call-site execution effects. Function values and function bodies are
//! deliberately separate: returning a closure never executes that closure.

use super::statement_facts::SynchronousCalls;
use super::value_flow::static_prop;
use super::*;

type Values = BTreeSet<AbstractValue>;
type Context = BTreeMap<Id, Values>;

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
enum AbstractValue {
    Origin(ValueOrigin),
    Closure(u32, Box<Context>),
    ObjectAssign,
    Undefined,
    NonCallable,
    Unknown,
}

#[derive(Debug, Clone)]
enum ValueExpr {
    Binding(Id),
    Origin(ValueOrigin),
    NonCallable,
    Member(Box<Self>, Option<String>),
    Call(Box<Self>, Vec<Self>),
    Alternatives(Vec<Self>),
    Unknown,
}

impl ValueExpr {
    fn from_expr(expr: &Expr) -> Self {
        match expr.unwrap_parens() {
            Expr::Ident(ident) => Self::Binding(ident.to_id()),
            Expr::Fn(function) => Self::Origin(ValueOrigin::Function(function.function.span.lo.0)),
            Expr::Arrow(arrow) => Self::Origin(ValueOrigin::Function(arrow.span.lo.0)),
            Expr::Class(class) => Self::Origin(ValueOrigin::Class(class.class.span.lo.0)),
            Expr::Object(object) => Self::Origin(ValueOrigin::Object(object.span.lo.0)),
            Expr::Array(array) => Self::Origin(ValueOrigin::Object(array.span.lo.0)),
            Expr::Member(member) => Self::Member(
                Box::new(Self::from_expr(&member.obj)),
                static_prop(&member.prop),
            ),
            Expr::Call(call) => match &call.callee {
                Callee::Expr(callee) => Self::Call(
                    Box::new(Self::from_expr(callee)),
                    call.args
                        .iter()
                        .map(|arg| Self::from_expr(&arg.expr))
                        .collect(),
                ),
                Callee::Import(_) | Callee::Super(_) => Self::Unknown,
            },
            Expr::Cond(cond) => Self::Alternatives(vec![
                Self::from_expr(&cond.cons),
                Self::from_expr(&cond.alt),
            ]),
            Expr::Bin(bin)
                if matches!(
                    bin.op,
                    BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing
                ) =>
            {
                Self::Alternatives(vec![
                    Self::from_expr(&bin.left),
                    Self::from_expr(&bin.right),
                ])
            }
            Expr::Seq(seq) => seq
                .exprs
                .last()
                .map_or(Self::Unknown, |expr| Self::from_expr(expr)),
            Expr::Assign(assign) => Self::from_expr(&assign.right),
            Expr::Lit(_) | Expr::Tpl(_) | Expr::This(_) => Self::NonCallable,
            _ => Self::Unknown,
        }
    }

    fn bindings(&self, result: &mut BTreeSet<Id>) {
        match self {
            Self::Binding(id) => {
                result.insert(id.clone());
            }
            Self::Member(object, _) => object.bindings(result),
            Self::Call(callee, args) => {
                callee.bindings(result);
                for arg in args {
                    arg.bindings(result);
                }
            }
            Self::Alternatives(options) => {
                for option in options {
                    option.bindings(result);
                }
            }
            Self::Origin(_) | Self::NonCallable | Self::Unknown => {}
        }
    }
}

#[derive(Debug, Clone)]
struct Parameter {
    binding: Option<Id>,
    default: Option<ValueExpr>,
}

impl Parameter {
    fn from_pattern(pattern: &Pat) -> Self {
        match pattern {
            Pat::Ident(binding) => Self {
                binding: Some(binding.id.to_id()),
                default: None,
            },
            Pat::Assign(assign) => Self {
                binding: match assign.left.as_ref() {
                    Pat::Ident(binding) => Some(binding.id.to_id()),
                    _ => None,
                },
                default: Some(ValueExpr::from_expr(&assign.right)),
            },
            _ => Self {
                binding: None,
                default: None,
            },
        }
    }
}

#[derive(Debug, Clone)]
struct Call {
    callee: ValueExpr,
    args: Vec<ValueExpr>,
    is_new: bool,
    caller: Option<u32>,
    owner: StatementOrdinal,
    plain_object_sources: bool,
}

fn plain_assign_source_property(property: &PropOrSpread) -> bool {
    let PropOrSpread::Prop(property) = property else {
        return false;
    };
    let name = match property.as_ref() {
        Prop::KeyValue(pair) => &pair.key,
        Prop::Shorthand(ident) => return ident.sym.as_ref() != "__proto__",
        _ => return false,
    };
    match name {
        PropName::Ident(ident) => ident.sym.as_ref() != "__proto__",
        PropName::Str(string) => string.value.to_string_lossy() != "__proto__",
        PropName::Num(_) | PropName::BigInt(_) => true,
        PropName::Computed(computed) => match computed.expr.unwrap_parens() {
            Expr::Lit(Lit::Str(string)) => string.value.to_string_lossy() != "__proto__",
            Expr::Lit(Lit::Num(_)) | Expr::Lit(Lit::BigInt(_)) => true,
            _ => false,
        },
    }
}

fn static_property_name(name: &PropName) -> Option<String> {
    match name {
        PropName::Ident(ident) => Some(ident.sym.to_string()),
        PropName::Str(string) => Some(string.value.to_string_lossy().into_owned()),
        PropName::Num(number) => Some(number.value.to_string()),
        PropName::BigInt(number) => Some(number.value.to_string()),
        PropName::Computed(computed) => match computed.expr.unwrap_parens() {
            Expr::Lit(Lit::Str(string)) => Some(string.value.to_string_lossy().into_owned()),
            Expr::Lit(Lit::Num(number)) => Some(number.value.to_string()),
            _ => None,
        },
    }
}

#[derive(Debug, Clone)]
struct Callable {
    owner: StatementOrdinal,
    params: Vec<Parameter>,
    reads: BTreeSet<Id>,
    rebinds: BTreeSet<Id>,
    calls: Vec<Call>,
    local_assignments: Vec<(Id, ValueExpr)>,
    returns: Vec<ValueExpr>,
    local_functions: BTreeSet<Id>,
    literal_captures: BTreeSet<Id>,
    escaped_bindings: BTreeSet<Id>,
    member_writes: Vec<(Id, String)>,
    dynamic_property_effect: bool,
    opaque: bool,
    generator: bool,
}

impl Default for Callable {
    fn default() -> Self {
        Self {
            owner: StatementOrdinal(0),
            params: Vec::new(),
            reads: BTreeSet::new(),
            rebinds: BTreeSet::new(),
            calls: Vec::new(),
            local_assignments: Vec::new(),
            returns: Vec::new(),
            local_functions: BTreeSet::new(),
            literal_captures: BTreeSet::new(),
            escaped_bindings: BTreeSet::new(),
            member_writes: Vec::new(),
            dynamic_property_effect: false,
            opaque: false,
            generator: false,
        }
    }
}

struct ExecutionIndex {
    owner: StatementOrdinal,
    current: Option<u32>,
    past_await: bool,
    may_skip_await_depth: u32,
    literal_depth: u32,
    callables: BTreeMap<u32, Callable>,
    setter_owners: BTreeMap<Option<String>, BTreeSet<StatementOrdinal>>,
    top_calls: Vec<Vec<Call>>,
    top_property_writes: Vec<Vec<(Option<Id>, Option<String>)>>,
    top_assignments: Vec<Vec<(Id, ValueExpr)>>,
    opaque_top: BTreeSet<StatementOrdinal>,
}

impl Default for ExecutionIndex {
    fn default() -> Self {
        Self {
            owner: StatementOrdinal(0),
            current: None,
            past_await: false,
            may_skip_await_depth: 0,
            literal_depth: 0,
            callables: BTreeMap::new(),
            setter_owners: BTreeMap::new(),
            top_calls: Vec::new(),
            top_property_writes: Vec::new(),
            top_assignments: Vec::new(),
            opaque_top: BTreeSet::new(),
        }
    }
}

impl ExecutionIndex {
    fn collect(body: &[TopLevelItemView<'_>]) -> Self {
        let mut index = Self {
            top_calls: vec![Vec::new(); body.len()],
            top_property_writes: vec![Vec::new(); body.len()],
            top_assignments: vec![Vec::new(); body.len()],
            ..Self::default()
        };
        for (ordinal, item) in body.iter().enumerate() {
            index.owner = StatementOrdinal(ordinal);
            index.past_await = false;
            index.may_skip_await_depth = 0;
            index.literal_depth = 0;
            item.as_module_item().visit_with(&mut index);
        }
        index
    }

    fn with_callable(&mut self, key: u32, params: Vec<Parameter>, visit: impl FnOnce(&mut Self)) {
        let saved_current = self.current.replace(key);
        let saved_await = std::mem::replace(&mut self.past_await, false);
        let saved_may_skip = std::mem::replace(&mut self.may_skip_await_depth, 0);
        let opaque_params = params.iter().any(|param| param.binding.is_none());
        self.callables.entry(key).or_insert_with(|| Callable {
            owner: self.owner,
            params,
            opaque: opaque_params,
            ..Callable::default()
        });
        visit(self);
        self.current = saved_current;
        self.past_await = saved_await;
        self.may_skip_await_depth = saved_may_skip;
    }

    fn record_call(&mut self, call: Call) {
        if self.current.is_some() && self.past_await {
            return;
        }
        if let Some(key) = self.current {
            self.callables.get_mut(&key).unwrap().calls.push(call);
        } else {
            self.top_calls[self.owner.0].push(call);
        }
    }

    fn mark_opaque(&mut self) {
        if self.current.is_some() && self.past_await {
            return;
        }
        if let Some(key) = self.current {
            self.callables.get_mut(&key).unwrap().opaque = true;
        } else {
            self.opaque_top.insert(self.owner);
        }
    }

    fn mark_dynamic_property_effect(&mut self) {
        if self.past_await {
            return;
        }
        if let Some(key) = self.current {
            self.callables
                .get_mut(&key)
                .unwrap()
                .dynamic_property_effect = true;
        } else {
            self.top_property_writes[self.owner.0].push((None, None));
        }
    }

    fn record_member_write(&mut self, member: &MemberExpr) {
        if self.past_await {
            return;
        }
        let receiver = match member.obj.unwrap_parens() {
            Expr::Ident(object) => Some(object.to_id()),
            _ => None,
        };
        let property = static_prop(&member.prop);
        match (self.current, receiver, property) {
            (Some(key), Some(receiver), Some(property)) => self
                .callables
                .get_mut(&key)
                .unwrap()
                .member_writes
                .push((receiver, property)),
            (None, receiver, property) => {
                self.top_property_writes[self.owner.0].push((receiver, property))
            }
            _ => self.mark_dynamic_property_effect(),
        }
    }

    fn visit_may_skip<N: VisitWith<Self>>(&mut self, node: &N) {
        let before = self.past_await;
        self.may_skip_await_depth += 1;
        node.visit_children_with(self);
        self.may_skip_await_depth -= 1;
        self.past_await = before;
    }
}

impl Visit for ExecutionIndex {
    fn visit_class_member(&mut self, member: &ClassMember) {
        // Defining a class evaluates computed keys and static initializers,
        // but does not run its constructor or instance field initializers.
        // The ordinary SWC descent would put calls in those deferred bodies
        // into `top_calls`, falsely promoting their callees at class init.
        match member {
            ClassMember::ClassProp(prop) => prop.decorators.visit_with(self),
            ClassMember::PrivateProp(prop) => prop.decorators.visit_with(self),
            ClassMember::AutoAccessor(accessor) => accessor.decorators.visit_with(self),
            _ => {}
        }
        visit_eager_member_parts(self, member);
        match member {
            ClassMember::Method(method) => {
                if method.kind == MethodKind::Setter {
                    self.setter_owners
                        .entry(static_property_name(&method.key))
                        .or_default()
                        .insert(self.owner);
                }
                method.function.visit_with(self);
            }
            ClassMember::PrivateMethod(method) => {
                method.function.visit_with(self);
            }
            ClassMember::Constructor(ctor) => {
                // Keep inspecting the deferred body for nested accessors;
                // property-write promotion still needs their owner. Its
                // calls must stay out of the class owner's top-call list.
                self.with_callable(ctor.span.lo.0, Vec::new(), |index| {
                    ctor.visit_children_with(index);
                });
            }
            ClassMember::ClassProp(prop) if !prop.is_static => {
                if let Some(value) = &prop.value {
                    self.with_callable(prop.span.lo.0, Vec::new(), |index| {
                        value.visit_with(index);
                    });
                }
            }
            ClassMember::PrivateProp(prop) if !prop.is_static => {
                if let Some(value) = &prop.value {
                    self.with_callable(prop.span.lo.0, Vec::new(), |index| {
                        value.visit_with(index);
                    });
                }
            }
            ClassMember::AutoAccessor(accessor) if !accessor.is_static => {
                if let Some(value) = &accessor.value {
                    self.with_callable(accessor.span.lo.0, Vec::new(), |index| {
                        value.visit_with(index);
                    });
                }
            }
            _ => {}
        }
    }

    fn visit_setter_prop(&mut self, setter: &SetterProp) {
        self.setter_owners
            .entry(static_property_name(&setter.key))
            .or_default()
            .insert(self.owner);
        setter.visit_children_with(self);
    }

    fn visit_object_lit(&mut self, object: &ObjectLit) {
        self.literal_depth += 1;
        object.visit_children_with(self);
        self.literal_depth -= 1;
    }

    fn visit_array_lit(&mut self, array: &ArrayLit) {
        self.literal_depth += 1;
        array.visit_children_with(self);
        self.literal_depth -= 1;
    }

    fn visit_fn_decl(&mut self, declaration: &FnDecl) {
        if let Some(key) = self.current {
            let callable = self.callables.get_mut(&key).unwrap();
            callable.local_functions.insert(declaration.ident.to_id());
            callable.local_assignments.push((
                declaration.ident.to_id(),
                ValueExpr::Origin(ValueOrigin::Function(declaration.function.span.lo.0)),
            ));
        }
        declaration.visit_children_with(self);
    }

    fn visit_if_stmt(&mut self, statement: &IfStmt) {
        statement.test.visit_with(self);
        let after_test = self.past_await;
        statement.cons.visit_with(self);
        let after_then = self.past_await;
        self.past_await = after_test;
        if let Some(else_branch) = &statement.alt {
            else_branch.visit_with(self);
            self.past_await &= after_then;
        } else {
            self.past_await = after_test;
        }
    }

    fn visit_cond_expr(&mut self, expression: &CondExpr) {
        expression.test.visit_with(self);
        let after_test = self.past_await;
        expression.cons.visit_with(self);
        let after_then = self.past_await;
        self.past_await = after_test;
        expression.alt.visit_with(self);
        self.past_await &= after_then;
    }

    fn visit_bin_expr(&mut self, expression: &BinExpr) {
        expression.left.visit_with(self);
        let after_left = self.past_await;
        expression.right.visit_with(self);
        if matches!(
            expression.op,
            BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing
        ) {
            self.past_await = after_left;
        }
    }

    fn visit_while_stmt(&mut self, node: &WhileStmt) {
        self.visit_may_skip(node);
    }
    fn visit_do_while_stmt(&mut self, node: &DoWhileStmt) {
        self.visit_may_skip(node);
    }
    fn visit_for_stmt(&mut self, node: &ForStmt) {
        self.visit_may_skip(node);
    }
    fn visit_for_in_stmt(&mut self, node: &ForInStmt) {
        self.visit_may_skip(node);
    }
    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
        self.visit_may_skip(node);
    }
    fn visit_try_stmt(&mut self, node: &TryStmt) {
        self.visit_may_skip(node);
    }
    fn visit_switch_stmt(&mut self, node: &SwitchStmt) {
        self.visit_may_skip(node);
    }

    fn visit_function(&mut self, function: &Function) {
        // Decorator expressions run when the method is defined. Defaults in
        // parameter patterns and the body run only when it is called.
        function.decorators.visit_with(self);
        for param in &function.params {
            param.decorators.visit_with(self);
        }
        let params = function
            .params
            .iter()
            .map(|param| Parameter::from_pattern(&param.pat))
            .collect();
        self.with_callable(function.span.lo.0, params, |index| {
            index
                .callables
                .get_mut(&function.span.lo.0)
                .unwrap()
                .generator = function.is_generator;
            for param in &function.params {
                param.pat.visit_with(index);
            }
            if let Some(body) = &function.body {
                body.visit_with(index);
            }
        });
    }

    fn visit_arrow_expr(&mut self, arrow: &ArrowExpr) {
        let params = arrow.params.iter().map(Parameter::from_pattern).collect();
        self.with_callable(arrow.span.lo.0, params, |index| {
            arrow.visit_children_with(index);
            if let ArrowFunctionBody::Expr(expr) = arrow.body.as_ref() {
                index
                    .callables
                    .get_mut(&arrow.span.lo.0)
                    .unwrap()
                    .returns
                    .push(ValueExpr::from_expr(expr));
            }
        });
    }

    fn visit_ident(&mut self, ident: &Ident) {
        if !self.past_await
            && let Some(key) = self.current
        {
            let callable = self.callables.get_mut(&key).unwrap();
            callable.reads.insert(ident.to_id());
            if self.literal_depth > 0 {
                callable.literal_captures.insert(ident.to_id());
            }
        }
    }

    fn visit_binding_ident(&mut self, _binding: &BindingIdent) {}

    fn visit_var_declarator(&mut self, declarator: &VarDeclarator) {
        declarator.visit_children_with(self);
        if let (Pat::Ident(binding), Some(init)) = (&declarator.name, &declarator.init) {
            let assignment = (binding.id.to_id(), ValueExpr::from_expr(init));
            if let Some(key) = self.current {
                self.callables
                    .get_mut(&key)
                    .unwrap()
                    .local_assignments
                    .push(assignment);
            } else {
                self.top_assignments[self.owner.0].push(assignment);
            }
        }
    }

    fn visit_assign_expr(&mut self, assignment: &AssignExpr) {
        let before_assignment = self.past_await;
        assignment.visit_children_with(self);
        if matches!(
            assignment.op,
            AssignOp::AndAssign | AssignOp::OrAssign | AssignOp::NullishAssign
        ) {
            self.past_await = before_assignment;
        }
        if let (Some(key), AssignTarget::Simple(SimpleAssignTarget::Ident(binding))) =
            (self.current, &assignment.left)
        {
            let callable = self.callables.get_mut(&key).unwrap();
            callable.rebinds.insert(binding.id.to_id());
            callable
                .local_assignments
                .push((binding.id.to_id(), ValueExpr::from_expr(&assignment.right)));
        } else if let AssignTarget::Simple(SimpleAssignTarget::Ident(binding)) = &assignment.left {
            self.top_assignments[self.owner.0]
                .push((binding.id.to_id(), ValueExpr::from_expr(&assignment.right)));
        } else if let AssignTarget::Simple(SimpleAssignTarget::Member(member)) = &assignment.left {
            if let Some(key) = self.current {
                ValueExpr::from_expr(&assignment.right)
                    .bindings(&mut self.callables.get_mut(&key).unwrap().escaped_bindings);
            }
            if assignment.op == AssignOp::Assign {
                self.record_member_write(member);
            } else {
                self.mark_dynamic_property_effect();
            }
        } else {
            self.mark_dynamic_property_effect();
        }
    }

    fn visit_update_expr(&mut self, expression: &UpdateExpr) {
        expression.visit_children_with(self);
        if matches!(expression.arg.unwrap_parens(), Expr::Member(_)) {
            self.mark_dynamic_property_effect();
        }
        self.mark_opaque();
    }

    fn visit_unary_expr(&mut self, expression: &UnaryExpr) {
        expression.visit_children_with(self);
        if expression.op == UnaryOp::Delete
            && matches!(expression.arg.unwrap_parens(), Expr::Member(_))
        {
            self.mark_dynamic_property_effect();
        }
    }

    fn visit_return_stmt(&mut self, ret: &ReturnStmt) {
        ret.visit_children_with(self);
        if let (Some(key), Some(arg)) = (self.current, &ret.arg) {
            if let ValueExpr::Binding(id) = ValueExpr::from_expr(arg) {
                self.callables
                    .get_mut(&key)
                    .unwrap()
                    .escaped_bindings
                    .insert(id);
            }
            self.callables
                .get_mut(&key)
                .unwrap()
                .returns
                .push(ValueExpr::from_expr(arg));
        }
    }

    fn visit_throw_stmt(&mut self, thrown: &ThrowStmt) {
        thrown.visit_children_with(self);
        if let Some(key) = self.current {
            ValueExpr::from_expr(&thrown.arg)
                .bindings(&mut self.callables.get_mut(&key).unwrap().escaped_bindings);
        }
    }

    fn visit_call_expr(&mut self, call: &CallExpr) {
        call.visit_children_with(self);
        if call.args.iter().any(|arg| arg.spread.is_some()) {
            self.mark_opaque();
        }
        if let Callee::Expr(callee) = &call.callee {
            self.record_call(Call {
                callee: ValueExpr::from_expr(callee),
                args: call
                    .args
                    .iter()
                    .map(|arg| ValueExpr::from_expr(&arg.expr))
                    .collect(),
                is_new: false,
                caller: self.current,
                owner: self.owner,
                plain_object_sources: call.args.len() > 1
                    && call.args.iter().skip(1).all(|arg| {
                        matches!(arg.expr.unwrap_parens(), Expr::Object(object)
                            if object.props.iter().all(plain_assign_source_property))
                    }),
            });
        } else {
            self.mark_opaque();
        }
    }

    fn visit_tagged_tpl(&mut self, template: &TaggedTpl) {
        template.visit_children_with(self);
        self.mark_opaque();
    }

    fn visit_opt_chain_expr(&mut self, chain: &OptChainExpr) {
        let before = self.past_await;
        chain.visit_children_with(self);
        self.past_await = before;
        self.mark_opaque();
    }

    fn visit_new_expr(&mut self, new_expr: &NewExpr) {
        new_expr.visit_children_with(self);
        if new_expr
            .args
            .as_deref()
            .unwrap_or_default()
            .iter()
            .any(|arg| arg.spread.is_some())
        {
            self.mark_opaque();
        }
        self.record_call(Call {
            callee: ValueExpr::from_expr(&new_expr.callee),
            args: new_expr
                .args
                .as_deref()
                .unwrap_or_default()
                .iter()
                .map(|arg| ValueExpr::from_expr(&arg.expr))
                .collect(),
            is_new: true,
            caller: self.current,
            owner: self.owner,
            plain_object_sources: false,
        });
    }

    fn visit_await_expr(&mut self, await_expr: &AwaitExpr) {
        await_expr.arg.visit_with(self);
        if self.may_skip_await_depth == 0 {
            self.past_await = true;
        }
    }
}

fn from_flow(values: BTreeSet<ValueOrigin>) -> Values {
    values
        .into_iter()
        .map(|value| match value {
            ValueOrigin::Unknown => AbstractValue::Unknown,
            other => AbstractValue::Origin(other),
        })
        .collect()
}

fn unknown() -> Values {
    BTreeSet::from([AbstractValue::Unknown])
}

#[derive(Debug, Clone, Default)]
struct Outcome {
    effects: SynchronousCalls,
    returns: Values,
    complete: bool,
    executed_owners: BTreeSet<StatementOrdinal>,
    unknown_call: bool,
}

struct Executor<'a> {
    index: &'a ExecutionIndex,
    facts: &'a [StatementFacts],
    flow: &'a ChunkValueFlow,
    shadowed: &'a BTreeSet<&'static str>,
    top_bindings: BTreeSet<Id>,
    binding_owner: BTreeMap<Id, StatementOrdinal>,
    prior_effect_owners: BTreeSet<StatementOrdinal>,
    prior_unknown_call: bool,
    mutable_captures: BTreeSet<Id>,
    active: BTreeSet<(u32, Vec<Values>, Context)>,
    cache: BTreeMap<(u32, Vec<Values>, Context), Outcome>,
}

impl Executor<'_> {
    fn promote_lazy_owner(&self, owner: StatementOrdinal, outcome: &mut Outcome) {
        let fact = &self.facts[owner.0];
        outcome.effects.reads.extend(
            fact.reads
                .lazy
                .iter()
                .filter(|id| self.top_bindings.contains(*id))
                .cloned()
                .map(|id| (id, owner)),
        );
        outcome.effects.rebinds.extend(
            fact.rebinds
                .lazy
                .iter()
                .filter(|id| self.top_bindings.contains(*id))
                .cloned()
                .map(|id| (id, owner)),
        );
    }

    fn promote_property_writes(
        &self,
        writes: &[(Option<Id>, Option<String>)],
        owner: StatementOrdinal,
        outcome: &mut Outcome,
    ) {
        if writes.is_empty() {
            return;
        }
        if self.index.setter_owners.is_empty() && !outcome.unknown_call && !self.prior_unknown_call
        {
            return;
        }
        let mut possible_owners = outcome.executed_owners.clone();
        possible_owners.insert(owner);
        possible_owners.extend(self.prior_effect_owners.iter().copied());
        for (receiver, _) in writes {
            if let Some(receiver) = receiver
                && let Some(binding_owner) = self.binding_owner.get(receiver)
            {
                possible_owners.insert(*binding_owner);
            }
        }
        let unknown_receiver = writes.iter().any(|(receiver, _)| receiver.is_none());
        if outcome.unknown_call || self.prior_unknown_call || unknown_receiver {
            possible_owners.extend(self.facts.iter().map(|fact| fact.ordinal));
        }
        let mut accessor_owners = BTreeSet::new();
        if outcome.unknown_call || self.prior_unknown_call {
            accessor_owners.extend(possible_owners);
        } else {
            for (_, property) in writes {
                if let Some(property) = property {
                    accessor_owners.extend(
                        self.index
                            .setter_owners
                            .get(&Some(property.clone()))
                            .into_iter()
                            .flatten()
                            .copied(),
                    );
                    accessor_owners.extend(
                        self.index
                            .setter_owners
                            .get(&None)
                            .into_iter()
                            .flatten()
                            .copied(),
                    );
                } else {
                    accessor_owners.extend(
                        self.index
                            .setter_owners
                            .values()
                            .flat_map(|owners| owners.iter().copied()),
                    );
                }
            }
            accessor_owners = accessor_owners
                .intersection(&possible_owners)
                .copied()
                .collect();
        }
        if !accessor_owners.is_empty() {
            for accessor_owner in accessor_owners {
                self.promote_lazy_owner(accessor_owner, outcome);
            }
            outcome.complete = false;
        }
    }

    fn safe_object_assign_target(&self, call: &Call, id: &Id) -> bool {
        if call.is_new
            || !call.plain_object_sources
            || !matches!(call.args.first(), Some(ValueExpr::Binding(target)) if target == id)
        {
            return false;
        }
        let ValueExpr::Member(object, Some(property)) = &call.callee else {
            return false;
        };
        matches!(object.as_ref(), ValueExpr::Binding(object_id)
            if object_id.0.as_ref() == "Object"
                && !self.flow.is_declared(object_id)
                && !self.shadowed.contains("Object")
                && property == "assign")
    }

    fn stable_fresh_function_target(&self, key: u32, callable: &Callable, id: &Id) -> bool {
        callable.local_functions.contains(id)
            && !callable.rebinds.contains(id)
            && !callable.literal_captures.contains(id)
            && !callable.escaped_bindings.contains(id)
            && !callable.dynamic_property_effect
            && callable
                .member_writes
                .iter()
                .all(|(receiver, property)| receiver != id || property != "__proto__")
            && !callable.local_assignments.iter().any(|(target, value)| {
                if target == id {
                    return false;
                }
                let mut mentioned = BTreeSet::new();
                value.bindings(&mut mentioned);
                mentioned.contains(id)
            })
            && !self
                .index
                .callables
                .iter()
                .any(|(other, nested)| *other != key && nested.reads.contains(id))
            && !callable.calls.iter().any(|call| {
                let mut mentioned = BTreeSet::new();
                call.callee.bindings(&mut mentioned);
                for arg in &call.args {
                    arg.bindings(&mut mentioned);
                }
                mentioned.contains(id) && !self.safe_object_assign_target(call, id)
            })
    }

    fn evaluate(&mut self, expr: &ValueExpr, env: &Context, captures: &Context) -> Values {
        match expr {
            ValueExpr::Binding(id) => env.get(id).cloned().unwrap_or_else(|| {
                let values = from_flow(self.flow.values_of_binding(id));
                if values.is_empty() { unknown() } else { values }
            }),
            ValueExpr::Origin(ValueOrigin::Function(key)) => {
                if captures.len() > 64
                    || captures.keys().any(|id| self.mutable_captures.contains(id))
                {
                    unknown()
                } else {
                    BTreeSet::from([AbstractValue::Closure(*key, Box::new(captures.clone()))])
                }
            }
            ValueExpr::Origin(origin) => BTreeSet::from([AbstractValue::Origin(*origin)]),
            ValueExpr::NonCallable => BTreeSet::from([AbstractValue::NonCallable]),
            ValueExpr::Member(object, property) => {
                if matches!(object.as_ref(), ValueExpr::Binding(id) if id.0.as_ref() == "Object" && !self.flow.is_declared(id))
                    && property.as_deref() == Some("assign")
                    && !self.shadowed.contains("Object")
                {
                    return BTreeSet::from([AbstractValue::ObjectAssign]);
                }
                let objects = self.evaluate(object, env, captures);
                let mut values = Values::new();
                for object in objects {
                    if let (AbstractValue::Origin(ValueOrigin::Object(object)), Some(property)) =
                        (object, property)
                    {
                        if let Some(found) = self.flow.values_of_property(object, property) {
                            values.extend(from_flow(found.clone()));
                        } else {
                            values.insert(AbstractValue::Unknown);
                        }
                    } else {
                        values.insert(AbstractValue::Unknown);
                    }
                }
                values
            }
            ValueExpr::Call(callee, args) => {
                let targets = self.evaluate(callee, env, captures);
                let args: Vec<Values> = args
                    .iter()
                    .map(|arg| self.evaluate(arg, env, captures))
                    .collect();
                let mut returns = Values::new();
                for target in targets {
                    match target {
                        AbstractValue::Origin(ValueOrigin::Function(key)) => {
                            returns.extend(self.invoke(key, args.clone(), &Context::new()).returns);
                        }
                        AbstractValue::Closure(key, captured) => {
                            returns.extend(self.invoke(key, args.clone(), &captured).returns);
                        }
                        AbstractValue::ObjectAssign => {
                            returns.extend(args.first().cloned().unwrap_or_else(unknown));
                        }
                        _ => {
                            returns.insert(AbstractValue::Unknown);
                        }
                    }
                }
                returns
            }
            ValueExpr::Alternatives(options) => options
                .iter()
                .flat_map(|option| self.evaluate(option, env, captures))
                .collect(),
            ValueExpr::Unknown => unknown(),
        }
    }

    fn invoke(&mut self, key: u32, args: Vec<Values>, captured: &Context) -> Outcome {
        if self.cache.len() + self.active.len() >= 4096 {
            return Outcome::default();
        }
        let signature = (key, args.clone(), captured.clone());
        if let Some(cached) = self.cache.get(&signature) {
            return cached.clone();
        }
        if !self.active.insert(signature.clone()) {
            return Outcome::default();
        }
        let Some(callable) = self.index.callables.get(&key) else {
            self.active.remove(&signature);
            return Outcome::default();
        };
        let mut env = captured.clone();
        for (index, param) in callable.params.iter().enumerate() {
            let mut values = args
                .get(index)
                .cloned()
                .unwrap_or_else(|| BTreeSet::from([AbstractValue::Undefined]));
            if let Some(default) = &param.default
                && values.contains(&AbstractValue::Undefined)
            {
                values.remove(&AbstractValue::Undefined);
                values.extend(self.evaluate(default, &env, &env));
            }
            if let Some(binding) = &param.binding {
                env.insert(binding.clone(), values);
            }
        }
        for (id, value) in &callable.local_assignments {
            let values = self.evaluate(value, &env, &env);
            env.entry(id.clone()).or_default().extend(values);
        }
        let mut outcome = Outcome {
            complete: true,
            ..Outcome::default()
        };
        outcome.executed_owners.insert(callable.owner);
        if callable.generator {
            // Generator parameter initializers run at call time, although
            // the body waits for the first iteration. Their effects are not
            // separated in the index, so retain the conservative path.
            outcome.complete = false;
            self.active.remove(&signature);
            self.cache.insert(signature, outcome.clone());
            return outcome;
        }
        outcome.complete &= !callable.opaque;
        for id in &callable.reads {
            if self.top_bindings.contains(id) {
                outcome.effects.reads.insert((id.clone(), callable.owner));
            }
        }
        for id in &callable.rebinds {
            if self.top_bindings.contains(id) {
                outcome.effects.rebinds.insert((id.clone(), callable.owner));
            }
        }
        for call in &callable.calls {
            self.execute_call(call, &env, &env, &mut outcome);
        }
        for expr in &callable.returns {
            outcome.returns.extend(self.evaluate(expr, &env, &env));
        }
        let mut writes: Vec<_> = callable
            .member_writes
            .iter()
            .filter(|(receiver, _)| !self.stable_fresh_function_target(key, callable, receiver))
            .map(|(receiver, property)| (Some(receiver.clone()), Some(property.clone())))
            .collect();
        if callable.dynamic_property_effect {
            writes.push((None, None));
        }
        self.promote_property_writes(&writes, callable.owner, &mut outcome);
        self.active.remove(&signature);
        self.cache.insert(signature, outcome.clone());
        outcome
    }

    fn execute_call(
        &mut self,
        call: &Call,
        env: &Context,
        captures: &Context,
        outcome: &mut Outcome,
    ) {
        if call.is_new
            && matches!(&call.callee, ValueExpr::Binding(id) if id.0.as_ref() == "WeakMap" && !self.flow.is_declared(id))
            && !self.shadowed.contains("WeakMap")
            && call.args.is_empty()
        {
            return;
        }
        let targets = self.evaluate(&call.callee, env, captures);
        let args: Vec<Values> = call
            .args
            .iter()
            .map(|arg| self.evaluate(arg, env, captures))
            .collect();
        if targets.is_empty() {
            outcome.complete = false;
            outcome.unknown_call = true;
        }
        for target in targets {
            match target {
                AbstractValue::ObjectAssign
                    if !call.is_new
                        && call.plain_object_sources
                        && matches!(call.args.first(), Some(ValueExpr::Binding(id)) if call.caller
                        .and_then(|caller| self.index.callables.get(&caller).map(|body| (caller, body)))
                        .is_some_and(|(caller, body)| self.stable_fresh_function_target(caller, body, id))) =>
                    {}
                AbstractValue::ObjectAssign if !call.is_new => {
                    // Once the target's descriptor/prototype integrity is
                    // unproven, copying a property may synchronously call a
                    // setter. Include nested lazy bodies of owners that ran
                    // before this call, as well as the owners of the source
                    // and target values. The first-order fallback excludes
                    // those bodies because merely creating a closure does
                    // not execute it.
                    let mut possible_accessors = outcome.executed_owners.clone();
                    possible_accessors.insert(call.owner);
                    if call.caller.is_none() {
                        possible_accessors.extend(self.prior_effect_owners.iter().copied());
                    }
                    for arg in &call.args {
                        let mut bindings = BTreeSet::new();
                        arg.bindings(&mut bindings);
                        possible_accessors.extend(
                            bindings
                                .iter()
                                .filter_map(|id| self.binding_owner.get(id).copied()),
                        );
                    }
                    if outcome.unknown_call || (call.caller.is_none() && self.prior_unknown_call) {
                        possible_accessors.extend(self.facts.iter().map(|fact| fact.ordinal));
                    }
                    for owner in possible_accessors {
                        self.promote_lazy_owner(owner, outcome);
                    }
                    outcome.complete = false;
                    outcome.unknown_call = true;
                }
                AbstractValue::Origin(ValueOrigin::Function(key)) if !call.is_new => {
                    let nested = self.invoke(key, args.clone(), &Context::new());
                    outcome.effects.reads.extend(nested.effects.reads);
                    outcome.effects.rebinds.extend(nested.effects.rebinds);
                    outcome.executed_owners.extend(nested.executed_owners);
                    outcome.unknown_call |= nested.unknown_call;
                    outcome.complete &= nested.complete;
                }
                AbstractValue::Closure(key, captured) if !call.is_new => {
                    let nested = self.invoke(key, args.clone(), &captured);
                    outcome.effects.reads.extend(nested.effects.reads);
                    outcome.effects.rebinds.extend(nested.effects.rebinds);
                    outcome.executed_owners.extend(nested.executed_owners);
                    outcome.unknown_call |= nested.unknown_call;
                    outcome.complete &= nested.complete;
                }
                _ => {
                    outcome.complete = false;
                    outcome.unknown_call = true;
                }
            }
        }
    }
}

pub(crate) fn annotate_synchronous_calls(
    body: &[TopLevelItemView<'_>],
    flow: &ChunkValueFlow,
    shadowed: &BTreeSet<&'static str>,
    facts: &mut [StatementFacts],
) {
    let index = ExecutionIndex::collect(body);
    // Every top-level call is executed by `top_calls`, regardless of whether
    // its result is retained. Keep an initializer's value only if a later
    // call may read it, directly or through another initializer/function.
    // This avoids re-evaluating thousands of unused factory results.
    let mut needed: BTreeSet<Id> = index
        .callables
        .values()
        .flat_map(|callable| callable.reads.iter().cloned())
        .collect();
    for calls in &index.top_calls {
        for call in calls {
            call.callee.bindings(&mut needed);
            for arg in &call.args {
                arg.bindings(&mut needed);
            }
        }
    }
    let mut dependencies: BTreeMap<Id, BTreeSet<Id>> = BTreeMap::new();
    for assignments in &index.top_assignments {
        for (id, expression) in assignments {
            expression.bindings(dependencies.entry(id.clone()).or_default());
        }
    }
    let mut pending: Vec<Id> = needed.iter().cloned().collect();
    while let Some(id) = pending.pop() {
        if let Some(inputs) = dependencies.get(&id) {
            for input in inputs {
                if needed.insert(input.clone()) {
                    pending.push(input.clone());
                }
            }
        }
    }
    let mut executor = Executor {
        index: &index,
        facts,
        flow,
        shadowed,
        top_bindings: facts
            .iter()
            .flat_map(|fact| fact.declared.iter().cloned())
            .collect(),
        binding_owner: facts
            .iter()
            .flat_map(|fact| fact.declared.iter().cloned().map(|id| (id, fact.ordinal)))
            .collect(),
        prior_effect_owners: BTreeSet::new(),
        prior_unknown_call: false,
        mutable_captures: index
            .callables
            .values()
            .flat_map(|callable| callable.rebinds.iter().cloned())
            .collect(),
        active: BTreeSet::new(),
        cache: BTreeMap::new(),
    };
    let mut top_env = Context::new();
    let mut top_captures = Context::new();
    let mut effects = Vec::with_capacity(facts.len());
    for (fact, (calls, assignments)) in facts
        .iter()
        .zip(index.top_calls.iter().zip(&index.top_assignments))
    {
        let mut outcome = Outcome {
            complete: true,
            ..Outcome::default()
        };
        for call in calls {
            executor.execute_call(call, &top_env, &top_captures, &mut outcome);
        }
        executor.promote_property_writes(
            &index.top_property_writes[fact.ordinal.0],
            fact.ordinal,
            &mut outcome,
        );
        outcome.complete &= !index.opaque_top.contains(&fact.ordinal);
        outcome.effects.complete = outcome.complete;
        let mut prior_effect_owners = outcome.executed_owners.clone();
        if !fact.purity.is_pure() {
            prior_effect_owners.insert(fact.ordinal);
        }
        executor.prior_effect_owners.extend(prior_effect_owners);
        executor.prior_unknown_call |= outcome.unknown_call;
        effects.push(outcome.effects);
        for (id, expression) in assignments {
            if !needed.contains(id) {
                continue;
            }
            let values = executor.evaluate(expression, &top_env, &top_captures);
            if !executor.top_bindings.contains(id) {
                top_captures
                    .entry(id.clone())
                    .or_default()
                    .extend(values.clone());
            }
            top_env.entry(id.clone()).or_default().extend(values);
        }
    }
    for (fact, effect) in facts.iter_mut().zip(effects) {
        fact.synchronous_calls = Some(effect);
    }
}
