//! One walk over the resolved SWC module that emits inclusion constraints.
//!
//! The walk is flow- and path-insensitive: every branch contributes. A code
//! unit is a top-level source unit or a function body; each constraint and
//! variable access is attributed to the unit whose code performs it.

use js_ast::{ParsedJsModule, split_var_declarators, str_value};
use rustc_hash::{FxHashMap, FxHashSet};
use swc_common::{Spanned, SyntaxContext};
use swc_ecma_ast::*;

use crate::helpers::{Helper, HelperFn, Prepass, prepass};
use crate::model::{
    ANY, Arg, Cx, CxId, EMPTY_NODE, ESC, ESC_NODE, ESCAPED_SINK, FnInfo, GETTERS, GLOBAL_NODE,
    INDEX, Model, Node, Obj, ObjKind, PropId, Reason, Root, SETTERS, SYMBOLS_NODE, THROWN, Unit,
    UnitInfo, Var, VarAccess, VarInfo, VarKind,
};

/// How object and array literals allocated inside function bodies are
/// abstracted. Top-level literals are always one object per site.
#[derive(Debug, Clone, Copy, PartialEq, Eq, clap::ValueEnum, serde::Serialize)]
#[serde(rename_all = "snake_case")]
#[value(rename_all = "snake_case")]
pub enum FunctionHeap {
    /// One abstract object per literal.
    Site,
    /// One object literal and one array literal per function.
    Function,
    /// One object literal and one array literal for all functions.
    Global,
}

pub struct Generated {
    pub model: Model,
    /// Byte spans of the top-level source units, by ordinal.
    pub statement_spans: Vec<(u32, u32)>,
    pub escaped_unit: Unit,
}

#[derive(Clone)]
struct Ctx {
    unit: Unit,
    this: Node,
    ret: Option<Node>,
    /// The innermost non-arrow function, which owns `arguments`.
    args_fn: Option<u32>,
    /// What `super` property reads resolve against.
    super_home: Option<Node>,
    /// What `super(...)` calls.
    super_class: Option<Node>,
    /// The unit is a top-level source unit.
    top: bool,
    /// The activation can suspend, so its declarations can run in a later
    /// statement than its call.
    suspendable: bool,
}

struct FnShape<'a> {
    params: Vec<&'a Pat>,
    body: Body<'a>,
    arrow: bool,
    is_async: bool,
    is_generator: bool,
    span_lo: u32,
    super_home: Option<Node>,
}

enum Body<'a> {
    Block(&'a [Stmt]),
    Expr(&'a Expr),
    Empty,
}

/// One specialization of a helper in progress: its own copies of the
/// helper's bindings, and the string constants its parameters hold.
struct Specialization<'a> {
    helper: &'a Helper<'a>,
    vars: FxHashMap<Id, Var>,
    constants: FxHashMap<Var, String>,
}

/// Helpers specialize into helpers at most this deep.
const SPECIALIZATION_DEPTH: usize = 3;

struct Generator<'a> {
    m: Model,
    prepass: &'a Prepass<'a>,
    specializations: Vec<Specialization<'a>>,
    vars: FxHashMap<Id, Var>,
    unresolved: SyntaxContext,
    ctx: Ctx,
    arguments: FxHashMap<u32, Obj>,
    /// Set just before generating a literal that is a direct child of a
    /// summarized literal: the parent's abstract object.
    summarized_parent: Option<Obj>,
    /// Literals whose direct literal children share one abstract object per
    /// kind, and those shared objects.
    summarized: FxHashSet<Obj>,
    summaries: FxHashMap<(Obj, bool), Obj>,
    function_heap: FunctionHeap,
    /// The abstract literal per function unit (or for all) and kind.
    function_literals: FxHashMap<(Unit, bool), Obj>,
    /// Variables with a write that is not provably a number.
    non_numeric: FxHashSet<Var>,
    /// Per numeric-looking write, the variables its value depends on.
    numeric_writes: Vec<(Var, Vec<Var>)>,
    /// Set by `computed_key` for a key that is a number if these variables
    /// are; taken by the load or store that uses the key.
    key_deps: Option<Vec<Var>>,
    /// Set just before writing an identifier whose value is a number if
    /// these variables are.
    numeric_hint: Option<Vec<Var>>,
    /// Set just before a store whose base is rooted at a global identifier.
    store_global_rooted: bool,
    /// Loads and stores to turn into element accesses if the variables
    /// prove numeric.
    pending_index: Vec<(CxId, Vec<Var>)>,
}

/// A literal with at least this many direct literal children is a data
/// table: its children share one abstract object per kind, recursively, so a
/// table of N records does not put N objects in every set it flows through.
const TABLE_CHILDREN: usize = 16;

fn is_literal(expr: &Expr) -> bool {
    matches!(strip_parens(expr), Expr::Object(_) | Expr::Array(_))
}

pub fn generate(parsed: &ParsedJsModule, function_heap: FunctionHeap) -> Generated {
    let body = &parsed.module.body;
    let mut statement_spans = Vec::new();
    for item in body {
        match split_var_declarators(item) {
            Some(declarators) => {
                statement_spans.extend(
                    declarators
                        .iter()
                        .map(|declarator| (declarator.span.lo.0, declarator.span.hi.0)),
                );
            }
            None => statement_spans.push((item.span().lo.0, item.span().hi.0)),
        }
    }
    let unresolved = SyntaxContext::empty().apply_mark(parsed.unresolved_mark);
    let prepass = prepass(&parsed.module, unresolved);
    let mut m = Model::new();
    let escaped_unit = statement_spans.len() as Unit;
    for _ in 0..=escaped_unit {
        m.units.push(UnitInfo::default());
    }
    let mut generator = Generator {
        m,
        prepass: &prepass,
        specializations: Vec::new(),
        vars: FxHashMap::default(),
        unresolved,
        ctx: Ctx {
            unit: 0,
            this: EMPTY_NODE,
            ret: None,
            args_fn: None,
            super_home: None,
            super_class: None,
            top: true,
            suspendable: false,
        },
        arguments: FxHashMap::default(),
        summarized_parent: None,
        summarized: FxHashSet::default(),
        summaries: FxHashMap::default(),
        function_heap,
        function_literals: FxHashMap::default(),
        non_numeric: FxHashSet::default(),
        numeric_writes: Vec::new(),
        key_deps: None,
        numeric_hint: None,
        store_global_rooted: false,
        pending_index: Vec::new(),
    };
    let mut ordinal = 0;
    for item in body {
        match split_var_declarators(item) {
            Some(declarators) => {
                let (kind, exported) = match item {
                    ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => (var.kind, false),
                    ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => match &export.decl {
                        Decl::Var(var) => (var.kind, true),
                        _ => unreachable!("only variable declarations split"),
                    },
                    _ => unreachable!("only variable declarations split"),
                };
                for declarator in declarators {
                    generator.ctx.unit = ordinal;
                    generator.declarator(kind, declarator);
                    if exported {
                        generator.export_pattern(&declarator.name);
                    }
                    ordinal += 1;
                }
            }
            None => {
                generator.ctx.unit = ordinal;
                generator.module_item(item);
                ordinal += 1;
            }
        }
    }
    generator.resolve_numeric_keys();
    generator.expand_accessors();
    Generated {
        model: generator.m,
        statement_spans,
        escaped_unit,
    }
}

fn pattern_idents<'a>(pat: &'a Pat, out: &mut Vec<&'a Ident>) {
    match pat {
        Pat::Ident(binding) => out.push(&binding.id),
        Pat::Array(array) => {
            for element in array.elems.iter().flatten() {
                pattern_idents(element, out);
            }
        }
        Pat::Rest(rest) => pattern_idents(&rest.arg, out),
        Pat::Object(object) => {
            for prop in &object.props {
                match prop {
                    ObjectPatProp::KeyValue(key_value) => pattern_idents(&key_value.value, out),
                    ObjectPatProp::Assign(assign) => out.push(&assign.key.id),
                    ObjectPatProp::Rest(rest) => pattern_idents(&rest.arg, out),
                }
            }
        }
        Pat::Assign(assign) => pattern_idents(&assign.left, out),
        Pat::Invalid(_) | Pat::Expr(_) => {}
    }
}

fn strip_parens(mut expr: &Expr) -> &Expr {
    while let Expr::Paren(paren) = expr {
        expr = &paren.expr;
    }
    expr
}

impl<'a> Generator<'a> {
    fn node(&mut self) -> Node {
        self.m.node()
    }

    fn cx(&mut self, cx: Cx, span_lo: u32) -> CxId {
        self.m.constraint(cx, self.ctx.unit, span_lo)
    }

    /// Whether `expr` always evaluates to a number, given that the returned
    /// variables only ever hold numbers. Numeric keys address array
    /// elements and never alias a named property.
    fn numeric(&mut self, expr: &Expr) -> Option<Vec<Var>> {
        match expr {
            Expr::Paren(paren) => self.numeric(&paren.expr),
            Expr::Lit(Lit::Num(_)) | Expr::Update(_) => Some(Vec::new()),
            Expr::Unary(unary) => {
                matches!(unary.op, UnaryOp::Minus | UnaryOp::Plus | UnaryOp::Tilde).then(Vec::new)
            }
            Expr::Bin(binary) => match binary.op {
                BinaryOp::Sub
                | BinaryOp::Mul
                | BinaryOp::Div
                | BinaryOp::Mod
                | BinaryOp::Exp
                | BinaryOp::BitOr
                | BinaryOp::BitXor
                | BinaryOp::BitAnd
                | BinaryOp::LShift
                | BinaryOp::RShift
                | BinaryOp::ZeroFillRShift => Some(Vec::new()),
                BinaryOp::Add => {
                    let mut deps = self.numeric(&binary.left)?;
                    deps.extend(self.numeric(&binary.right)?);
                    Some(deps)
                }
                _ => None,
            },
            Expr::Cond(cond) => {
                let mut deps = self.numeric(&cond.cons)?;
                deps.extend(self.numeric(&cond.alt)?);
                Some(deps)
            }
            Expr::Seq(seq) => self.numeric(seq.exprs.last()?),
            Expr::Assign(assign) => match assign.op {
                AssignOp::Assign => self.numeric(&assign.right),
                AssignOp::AddAssign
                | AssignOp::AndAssign
                | AssignOp::OrAssign
                | AssignOp::NullishAssign => None,
                _ => Some(Vec::new()),
            },
            Expr::Ident(ident) if ident.ctxt != self.unresolved => Some(vec![self.var(ident)]),
            _ => None,
        }
    }

    /// Turns pending computed-key accesses into element accesses where the
    /// key's variables only ever hold numbers.
    fn resolve_numeric_keys(&mut self) {
        let mut numeric: FxHashSet<Var> = self
            .numeric_writes
            .iter()
            .map(|(var, _)| *var)
            .filter(|var| !self.non_numeric.contains(var))
            .collect();
        loop {
            let before = numeric.len();
            for (var, deps) in &self.numeric_writes {
                if deps.iter().any(|dep| !numeric.contains(dep)) {
                    numeric.remove(var);
                }
            }
            if numeric.len() == before {
                break;
            }
        }
        for (cx, deps) in std::mem::take(&mut self.pending_index) {
            if deps.iter().all(|dep| numeric.contains(dep)) {
                match &mut self.m.cxs[cx as usize].cx {
                    Cx::Load { prop, .. } | Cx::Store { prop, .. } => *prop = INDEX,
                    _ => unreachable!("only loads and stores are pending"),
                }
            }
        }
    }

    fn havoc(&mut self) {
        self.m.units[self.ctx.unit as usize].havoc = true;
    }

    fn opaque(&mut self, reason: Reason) {
        self.m.units[self.ctx.unit as usize].static_reasons |= reason.bit();
    }

    fn escape(&mut self, node: Node) {
        self.m.edge(node, ESCAPED_SINK);
    }

    fn union(&mut self, parts: &[Node]) -> Node {
        let node = self.node();
        for part in parts {
            self.m.edge(*part, node);
        }
        node
    }

    fn alloc(&mut self, kind: ObjKind, span_lo: u32) -> Obj {
        self.m.alloc(kind, self.ctx.unit, span_lo)
    }

    /// The object for a literal: the summary its parent prescribes, or a
    /// fresh site. Returns the born set to intersect with when reused, and
    /// whether this literal's own literal children are summarized.
    fn literal(
        &mut self,
        array: bool,
        literal_children: usize,
        span_lo: u32,
    ) -> (Obj, Option<FxHashSet<PropId>>, bool) {
        let kind = if array {
            ObjKind::Array
        } else {
            ObjKind::Plain
        };
        let function_key = match self.function_heap {
            _ if self.ctx.top => None,
            FunctionHeap::Site => None,
            FunctionHeap::Function => Some((self.ctx.unit, array)),
            FunctionHeap::Global => Some((Unit::MAX, array)),
        };
        let shared = match (self.summarized_parent.take(), function_key) {
            (_, Some(key)) => Some(self.function_literals.get(&key).copied().ok_or(key)),
            (Some(parent), None) => Some(
                self.summaries
                    .get(&(parent, array))
                    .copied()
                    .ok_or((parent, array)),
            ),
            (None, None) => None,
        };
        let (obj, earlier_born) = match shared {
            Some(Ok(obj)) => {
                let born = std::mem::take(&mut self.m.objs[obj as usize].born);
                (obj, Some(born))
            }
            Some(Err(key)) => {
                let obj = self.alloc(kind, span_lo);
                if function_key.is_some() {
                    self.function_literals.insert(key, obj);
                } else {
                    self.summaries.insert(key, obj);
                    self.summarized.insert(obj);
                }
                (obj, None)
            }
            None => (self.alloc(kind, span_lo), None),
        };
        if literal_children >= TABLE_CHILDREN {
            self.summarized.insert(obj);
        }
        (obj, earlier_born, self.summarized.contains(&obj))
    }

    /// Generates a literal's direct child.
    fn literal_child(&mut self, parent: Obj, summarize: bool, child: &Expr) -> Node {
        if summarize && is_literal(child) {
            self.summarized_parent = Some(parent);
        }
        let value = self.expr(child);
        self.summarized_parent = None;
        value
    }

    fn var(&mut self, ident: &Ident) -> Var {
        let id = ident.to_id();
        // A helper's own bindings are fresh in each specialization.
        let scope = self
            .specializations
            .iter()
            .rposition(|specialization| specialization.helper.locals.contains(&id));
        let known = match scope {
            Some(scope) => self.specializations[scope].vars.get(&id),
            None => self.vars.get(&id),
        };
        if let Some(var) = known {
            return *var;
        }
        let node = self.node();
        let var = self.m.vars.len() as Var;
        self.m.vars.push(VarInfo {
            node,
            name: ident.sym.to_string(),
            kind: VarKind::Local,
            exported: false,
            top_level: false,
        });
        match scope {
            Some(scope) => self.specializations[scope].vars.insert(id, var),
            None => self.vars.insert(id, var),
        };
        var
    }

    /// The string `expr` always evaluates to, where a helper's parameters
    /// hold the string literals of the call being specialized.
    fn constant_string(&mut self, expr: &Expr) -> Option<String> {
        match expr {
            Expr::Paren(paren) => self.constant_string(&paren.expr),
            Expr::Lit(Lit::Str(value)) => Some(str_value(value)),
            Expr::Tpl(template) if template.exprs.is_empty() => {
                let [quasi] = template.quasis.as_slice() else {
                    return None;
                };
                quasi
                    .cooked
                    .as_ref()
                    .and_then(|cooked| cooked.as_str().map(str::to_string))
            }
            Expr::Ident(ident) if ident.ctxt != self.unresolved => {
                let id = ident.to_id();
                self.specializations
                    .iter()
                    .rev()
                    .find_map(|specialization| {
                        let var = specialization.vars.get(&id)?;
                        specialization.constants.get(var).cloned()
                    })
            }
            // `key + ""`.
            Expr::Bin(BinExpr {
                op: BinaryOp::Add,
                left,
                right,
                ..
            }) => match (self.constant_string(left), self.constant_string(right)) {
                (Some(left), Some(right)) => Some(left + &right),
                _ => None,
            },
            Expr::Cond(cond) => {
                let cons = self.constant_string(&cond.cons)?;
                (self.constant_string(&cond.alt)? == cons).then_some(cons)
            }
            _ => None,
        }
    }

    /// Generates a helper's body for one call, in the caller's unit.
    fn specialize(&mut self, helper: &'a Helper<'a>, args: &[ExprOrSpread], span_lo: u32) -> Node {
        let constants: Vec<Option<String>> = args
            .iter()
            .map(|arg| self.constant_string(&arg.expr))
            .collect();
        let values: Vec<Node> = args.iter().map(|arg| self.expr(&arg.expr)).collect();
        let ret = self.node();
        let outer = self.ctx.clone();
        self.ctx = Ctx {
            this: EMPTY_NODE,
            ret: Some(ret),
            args_fn: None,
            super_home: None,
            super_class: None,
            top: false,
            suspendable: false,
            ..outer.clone()
        };
        self.specializations.push(Specialization {
            helper,
            vars: FxHashMap::default(),
            constants: FxHashMap::default(),
        });
        let params: Vec<&Pat> = match helper.function {
            HelperFn::Arrow(arrow) => arrow.params.iter().collect(),
            HelperFn::Function(function) => {
                function.params.iter().map(|param| &param.pat).collect()
            }
        };
        for (position, pat) in params.into_iter().enumerate() {
            let value = values.get(position).copied().unwrap_or(EMPTY_NODE);
            match pat {
                Pat::Ident(binding) => {
                    let var = self.var(&binding.id);
                    if let Some(Some(constant)) = constants.get(position) {
                        self.specializations
                            .last_mut()
                            .expect("pushed above")
                            .constants
                            .insert(var, constant.clone());
                    }
                    self.bind(pat, value, true);
                }
                Pat::Rest(rest) => {
                    let obj = self.alloc(ObjKind::Array, span_lo);
                    for value in values.iter().skip(position) {
                        self.m.store_value(obj, INDEX, *value);
                    }
                    let node = self.m.obj_node(obj);
                    self.bind(&rest.arg, node, true);
                }
                other => self.bind(other, value, true),
            }
        }
        match helper.function {
            HelperFn::Arrow(arrow) => match &*arrow.body {
                ArrowFunctionBody::FunctionBody(block) => self.stmts(&block.stmts),
                ArrowFunctionBody::Expr(expr) => {
                    let value = self.expr(expr);
                    self.m.edge(value, ret);
                }
            },
            HelperFn::Function(function) => {
                if let Some(body) = &function.body {
                    self.stmts(&body.stmts);
                }
            }
        }
        self.specializations.pop();
        self.ctx = outer;
        ret
    }

    /// `Object.defineProperty(target, key, { value | get | set, ... })` with
    /// a literal descriptor, as the store or accessor definition it is.
    /// Relies on `Object.defineProperty` being the intrinsic.
    fn define_property(&mut self, args: &[ExprOrSpread], span_lo: u32) -> Option<Node> {
        let [target, key, descriptor] = args else {
            return None;
        };
        if args.iter().any(|arg| arg.spread.is_some()) {
            return None;
        }
        let Expr::Object(descriptor) = strip_parens(&descriptor.expr) else {
            return None;
        };
        let mut fields = Vec::new();
        for prop in &descriptor.props {
            let PropOrSpread::Prop(prop) = prop else {
                return None;
            };
            let Prop::KeyValue(KeyValueProp {
                key: PropName::Ident(name),
                value,
            }) = &**prop
            else {
                return None;
            };
            if !matches!(
                &*name.sym,
                "value" | "get" | "set" | "enumerable" | "configurable" | "writable"
            ) {
                return None;
            }
            fields.push((&*name.sym, &**value));
        }
        let target = self.expr(&target.expr);
        let (prop, key) = self.computed_key(&key.expr);
        let key_deps = self.key_deps.take();
        // The definition replaces whatever the property was.
        self.key_deps = key_deps.clone();
        self.store(target, prop, key, EMPTY_NODE, span_lo);
        for (name, value) in fields {
            let value = self.expr(value);
            match name {
                "value" => {
                    self.key_deps = key_deps.clone();
                    self.store(target, prop, key, value, span_lo);
                }
                "get" | "set" => {
                    let getter = name == "get";
                    let (names, all) = if getter {
                        (&mut self.m.getter_names, GETTERS)
                    } else {
                        (&mut self.m.setter_names, SETTERS)
                    };
                    names.insert(prop);
                    self.cx(
                        Cx::Store {
                            base: target,
                            prop: all,
                            src: value,
                            global_rooted: false,
                        },
                        span_lo,
                    );
                    if prop != ANY && prop != INDEX {
                        let accessor = self.accessor_prop(prop, getter);
                        self.cx(
                            Cx::Store {
                                base: target,
                                prop: accessor,
                                src: value,
                                global_rooted: false,
                            },
                            span_lo,
                        );
                    }
                }
                _ => {}
            }
        }
        Some(target)
    }

    /// A call the generator resolves itself: a specialized helper, or a
    /// recognized `Object` function.
    fn known_call(&mut self, callee: &Expr, args: &[ExprOrSpread], span_lo: u32) -> Option<Node> {
        if args.iter().any(|arg| arg.spread.is_some()) {
            return None;
        }
        let prepass = self.prepass;
        let object_function = match strip_parens(callee) {
            Expr::Ident(ident) if ident.ctxt != self.unresolved => {
                let id = ident.to_id();
                if let Some(helper) = prepass.helpers.get(&id)
                    && self.specializations.len() < SPECIALIZATION_DEPTH
                {
                    return Some(self.specialize(helper, args, span_lo));
                }
                prepass.object_aliases.get(&id).map(String::as_str)
            }
            Expr::Member(MemberExpr {
                obj,
                prop: MemberProp::Ident(name),
                ..
            }) if matches!(&**obj, Expr::Ident(object)
                if object.ctxt == self.unresolved && &*object.sym == "Object") =>
            {
                Some(&*name.sym)
            }
            _ => None,
        };
        match object_function {
            Some("defineProperty") => self.define_property(args, span_lo),
            _ => None,
        }
    }

    fn access(&mut self, var: Var, write: bool) {
        self.m.var_accesses.push(VarAccess {
            unit: self.ctx.unit,
            var,
            write,
        });
    }

    fn read_ident(&mut self, ident: &Ident) -> Node {
        if ident.ctxt == self.unresolved {
            return match &*ident.sym {
                "undefined" | "NaN" | "Infinity" => EMPTY_NODE,
                "arguments" if self.ctx.args_fn.is_some() => self.arguments_object(ident.span.lo.0),
                "Symbol" => {
                    self.m.units[self.ctx.unit as usize].reads_unknown_heap = true;
                    SYMBOLS_NODE
                }
                _ => {
                    self.m.units[self.ctx.unit as usize].reads_unknown_heap = true;
                    GLOBAL_NODE
                }
            };
        }
        let var = self.var(ident);
        self.access(var, false);
        if self.m.vars[var as usize].kind == VarKind::Import {
            self.m.units[self.ctx.unit as usize].reads_unknown_heap = true;
        }
        self.m.vars[var as usize].node
    }

    /// `declaration`: this write initializes the binding where it is declared.
    fn write_ident(&mut self, ident: &Ident, value: Node, declaration: bool) {
        if ident.ctxt == self.unresolved {
            self.numeric_hint = None;
            self.m.units[self.ctx.unit as usize].writes_unknown_heap = true;
            self.escape(value);
            return;
        }
        let var = self.var(ident);
        match self.numeric_hint.take() {
            Some(deps) => self.numeric_writes.push((var, deps)),
            None => {
                self.non_numeric.insert(var);
            }
        }
        // A fresh activation's own initialization is invisible to every other
        // statement unless the activation can suspend.
        if !declaration || self.ctx.top || self.ctx.suspendable {
            self.access(var, true);
        }
        if declaration && self.ctx.top {
            self.m.vars[var as usize].top_level = true;
        }
        let node = self.m.vars[var as usize].node;
        self.m.edge(value, node);
    }

    fn arguments_object(&mut self, span_lo: u32) -> Node {
        let index = self.ctx.args_fn.expect("checked by the caller");
        let obj = match self.arguments.get(&index) {
            Some(obj) => *obj,
            None => {
                let obj = self.alloc(ObjKind::Array, span_lo);
                let all_args = self.m.fns[index as usize].all_args;
                self.m.store_value(obj, INDEX, all_args);
                self.arguments.insert(index, obj);
                obj
            }
        };
        self.m.obj_node(obj)
    }

    fn export_pattern(&mut self, pat: &Pat) {
        let mut idents = Vec::new();
        pattern_idents(pat, &mut idents);
        for ident in idents {
            self.export_ident(ident);
        }
    }

    fn export_ident(&mut self, ident: &Ident) {
        if ident.ctxt == self.unresolved {
            return;
        }
        let var = self.var(ident);
        self.m.vars[var as usize].exported = true;
        let node = self.m.vars[var as usize].node;
        self.escape(node);
    }

    fn module_item(&mut self, item: &ModuleItem) {
        match item {
            ModuleItem::Stmt(stmt) => self.stmt(stmt),
            ModuleItem::ModuleDecl(decl) => match decl {
                ModuleDecl::Import(import) => {
                    for specifier in &import.specifiers {
                        let local = match specifier {
                            ImportSpecifier::Named(named) => &named.local,
                            ImportSpecifier::Default(default) => &default.local,
                            ImportSpecifier::Namespace(namespace) => &namespace.local,
                        };
                        let var = self.var(local);
                        let info = &mut self.m.vars[var as usize];
                        info.kind = VarKind::Import;
                        info.top_level = true;
                        let node = info.node;
                        self.m.add(node, ESC);
                    }
                }
                ModuleDecl::ExportDecl(export) => {
                    self.decl(&export.decl);
                    match &export.decl {
                        Decl::Class(class) => self.export_ident(&class.ident),
                        Decl::Fn(function) => self.export_ident(&function.ident),
                        Decl::Var(var) => {
                            for declarator in &var.decls {
                                self.export_pattern(&declarator.name);
                            }
                        }
                        _ => {}
                    }
                }
                ModuleDecl::ExportNamed(named) => {
                    if named.src.is_none() {
                        for specifier in &named.specifiers {
                            match specifier {
                                ExportSpecifier::Named(ExportNamedSpecifier {
                                    orig: ModuleExportName::Ident(ident),
                                    ..
                                }) => self.export_ident(ident),
                                _ => self.havoc(),
                            }
                        }
                    }
                }
                ModuleDecl::ExportDefaultDecl(default) => {
                    let value = match &default.decl {
                        DefaultDecl::Class(class) => {
                            self.class_expr(class.ident.as_ref(), &class.class)
                        }
                        DefaultDecl::Fn(function) => {
                            self.fn_expr(function.ident.as_ref(), &function.function)
                        }
                        DefaultDecl::TsInterfaceDecl(_) => {
                            self.havoc();
                            EMPTY_NODE
                        }
                    };
                    self.escape(value);
                }
                ModuleDecl::ExportDefaultExpr(default) => {
                    let value = self.expr(&default.expr);
                    self.escape(value);
                }
                ModuleDecl::ExportAll(_) => {}
                _ => self.havoc(),
            },
        }
    }

    fn declarator(&mut self, kind: VarDeclKind, declarator: &VarDeclarator) {
        match &declarator.init {
            Some(init) => {
                let value = self.expr(init);
                if matches!(declarator.name, Pat::Ident(_)) {
                    self.numeric_hint = self.numeric(init);
                }
                self.bind(&declarator.name, value, true);
            }
            // `let x;` initializes the binding; `var x;` does nothing.
            None if kind != VarDeclKind::Var => self.bind(&declarator.name, EMPTY_NODE, true),
            None => {}
        }
    }

    fn decl(&mut self, decl: &Decl) {
        match decl {
            Decl::Var(var) => {
                for declarator in &var.decls {
                    self.declarator(var.kind, declarator);
                }
            }
            Decl::Fn(function) => {
                // Hoisted: the binding holds the function before any
                // statement runs, so the declaration is no write.
                let value = self.function_value(&function.function, None);
                let var = self.var(&function.ident);
                if self.ctx.top {
                    self.m.vars[var as usize].top_level = true;
                }
                let node = self.m.vars[var as usize].node;
                self.m.edge(value, node);
            }
            Decl::Class(class) => {
                let value = self.class(&class.class);
                self.write_ident(&class.ident, value, true);
            }
            _ => self.havoc(),
        }
    }

    fn stmts(&mut self, stmts: &[Stmt]) {
        for stmt in stmts {
            self.stmt(stmt);
        }
    }

    fn stmt(&mut self, stmt: &Stmt) {
        match stmt {
            Stmt::Block(block) => self.stmts(&block.stmts),
            Stmt::Empty(_) | Stmt::Debugger(_) | Stmt::Break(_) | Stmt::Continue(_) => {}
            Stmt::With(_) => self.havoc(),
            Stmt::Return(ret) => {
                if let Some(arg) = &ret.arg {
                    let value = self.expr(arg);
                    match self.ctx.ret {
                        Some(ret) => self.m.edge(value, ret),
                        None => self.havoc(),
                    }
                }
            }
            Stmt::Labeled(labeled) => self.stmt(&labeled.body),
            Stmt::If(branch) => {
                self.expr(&branch.test);
                self.stmt(&branch.cons);
                if let Some(alt) = &branch.alt {
                    self.stmt(alt);
                }
            }
            Stmt::Switch(switch) => {
                self.expr(&switch.discriminant);
                for case in &switch.cases {
                    if let Some(test) = &case.test {
                        self.expr(test);
                    }
                    self.stmts(&case.cons);
                }
            }
            Stmt::Throw(throw) => {
                let value = self.expr(&throw.arg);
                self.m.edge(value, THROWN);
            }
            Stmt::Try(attempt) => {
                self.stmts(&attempt.block.stmts);
                if let Some(handler) = &attempt.handler {
                    if let Some(param) = &handler.param {
                        // Anything thrown here, or by unknown code.
                        let caught = self.union(&[THROWN, ESC_NODE]);
                        self.bind(param, caught, true);
                    }
                    self.stmts(&handler.body.stmts);
                }
                if let Some(finalizer) = &attempt.finalizer {
                    self.stmts(&finalizer.stmts);
                }
            }
            Stmt::While(looped) => {
                self.expr(&looped.test);
                self.stmt(&looped.body);
            }
            Stmt::DoWhile(looped) => {
                self.stmt(&looped.body);
                self.expr(&looped.test);
            }
            Stmt::For(looped) => {
                match &looped.init {
                    Some(VarDeclOrExpr::VarDecl(var)) => {
                        for declarator in &var.decls {
                            self.declarator(var.kind, declarator);
                        }
                    }
                    Some(VarDeclOrExpr::Expr(expr)) => {
                        self.expr(expr);
                    }
                    None => {}
                }
                if let Some(test) = &looped.test {
                    self.expr(test);
                }
                if let Some(update) = &looped.update {
                    self.expr(update);
                }
                self.stmt(&looped.body);
            }
            Stmt::ForIn(looped) => {
                let object = self.expr(&looped.right);
                // Enumerating keys runs code only on an unknown object.
                self.cx(Cx::Touch { val: object }, looped.span.lo.0);
                self.for_head(&looped.left, EMPTY_NODE);
                self.stmt(&looped.body);
            }
            Stmt::ForOf(looped) => {
                let iterable = self.expr(&looped.right);
                let mut elements = self.iterate(iterable, looped.span.lo.0);
                if looped.is_await {
                    self.opaque(Reason::Suspension);
                    self.escape(elements);
                    elements = self.union(&[elements, ESC_NODE]);
                    if self.ctx.top {
                        self.havoc();
                    }
                }
                self.for_head(&looped.left, elements);
                self.stmt(&looped.body);
            }
            Stmt::Decl(decl) => self.decl(decl),
            Stmt::Expr(expr) => {
                self.expr(&expr.expr);
            }
        }
    }

    fn for_head(&mut self, head: &ForHead, value: Node) {
        match head {
            ForHead::VarDecl(var) => {
                for declarator in &var.decls {
                    self.bind(&declarator.name, value, true);
                }
            }
            ForHead::Pat(pat) => self.bind(pat, value, false),
            ForHead::UsingDecl(_) => self.havoc(),
        }
    }

    fn iterate(&mut self, value: Node, span_lo: u32) -> Node {
        self.m.iterate(value, self.ctx.unit, span_lo)
    }

    fn coerce(&mut self, value: Node, span_lo: u32) {
        self.m.coerce(value, self.ctx.unit, span_lo);
    }

    fn load(&mut self, base: Node, prop: PropId, root: Root, span_lo: u32) -> Node {
        if self.m.props.name(prop) == "__proto__" {
            self.havoc();
        }
        let dst = self.node();
        let cx = self.cx(
            Cx::Load {
                base,
                prop,
                dst,
                root,
            },
            span_lo,
        );
        if let Some(deps) = self.key_deps.take() {
            self.pending_index.push((cx, deps));
        }
        dst
    }

    fn store(&mut self, base: Node, prop: PropId, key: Option<Node>, src: Node, span_lo: u32) {
        if self.m.props.name(prop) == "__proto__" {
            self.havoc();
        }
        let global_rooted = std::mem::take(&mut self.store_global_rooted);
        let cx = self.cx(
            Cx::Store {
                base,
                prop,
                src,
                global_rooted,
            },
            span_lo,
        );
        if let Some(deps) = self.key_deps.take() {
            self.pending_index.push((cx, deps));
        }
        if let Some(key) = key {
            self.exotic_when_object_key(key, base, span_lo);
        }
    }

    /// A key that may be a symbol can install protocol methods
    /// (`Symbol.iterator`, `Symbol.toPrimitive`).
    fn exotic_when_object_key(&mut self, key: Node, target: Node, span_lo: u32) {
        let taint = self
            .m
            .constraint(Cx::ExoticKey { target }, self.ctx.unit, span_lo);
        self.m.subscribe(key, taint);
    }

    /// A property key: its name, and the key value when it is computed and
    /// not a literal.
    /// The property a string key names: numeric strings address elements.
    fn string_key(&mut self, key: &str) -> PropId {
        if key.parse::<f64>().is_ok() {
            INDEX
        } else {
            self.m.props.intern(key)
        }
    }

    fn computed_key(&mut self, expr: &Expr) -> (PropId, Option<Node>) {
        self.key_deps = None;
        match strip_parens(expr) {
            Expr::Lit(Lit::Str(value)) => (self.string_key(&str_value(value)), None),
            Expr::Lit(Lit::Num(_)) => (INDEX, None),
            Expr::Member(MemberExpr {
                obj,
                prop: MemberProp::Ident(name),
                ..
            }) if matches!(&**obj, Expr::Ident(symbol)
                if symbol.ctxt == self.unresolved && &*symbol.sym == "Symbol") =>
            {
                (self.m.props.intern(&format!("@@{}", name.sym)), None)
            }
            other => {
                let key = self.expr(other);
                if let Some(constant) = self.constant_string(other) {
                    return (self.string_key(&constant), None);
                }
                let converted = self.m.coerce(key, self.ctx.unit, other.span().lo.0);
                match self.numeric(other) {
                    Some(deps) if deps.is_empty() => (INDEX, None),
                    deps => {
                        // The key itself, or what converting an object key
                        // yields.
                        let key = self.union(&[key, converted]);
                        self.key_deps = deps;
                        (ANY, Some(key))
                    }
                }
            }
        }
    }

    fn prop_name(&mut self, name: &PropName) -> (PropId, Option<Node>) {
        match name {
            PropName::Ident(ident) => (self.string_key(&ident.sym), None),
            PropName::Str(value) => (self.string_key(&str_value(value)), None),
            PropName::Num(_) | PropName::BigInt(_) => (INDEX, None),
            PropName::Computed(computed) => self.computed_key(&computed.expr),
        }
    }

    fn private_name(&mut self, name: &PrivateName) -> PropId {
        self.m.props.intern(&format!("#{}", name.name))
    }

    fn member_prop(&mut self, prop: &MemberProp) -> (PropId, Option<Node>) {
        match prop {
            MemberProp::Ident(ident) => (self.string_key(&ident.sym), None),
            MemberProp::PrivateName(name) => (self.private_name(name), None),
            MemberProp::Computed(computed) => self.computed_key(&computed.expr),
        }
    }

    fn super_prop(&mut self, prop: &SuperProp) -> (PropId, Option<Node>) {
        match prop {
            SuperProp::Ident(ident) => (self.string_key(&ident.sym), None),
            SuperProp::Computed(computed) => self.computed_key(&computed.expr),
        }
    }

    fn super_home(&mut self) -> Node {
        match self.ctx.super_home {
            Some(home) => home,
            None => {
                self.havoc();
                ESC_NODE
            }
        }
    }

    fn root(&mut self, expr: &Expr) -> Root {
        match expr {
            Expr::Paren(paren) => self.root(&paren.expr),
            Expr::Member(member) => self.root(&member.obj),
            Expr::Call(CallExpr {
                callee: Callee::Expr(callee),
                ..
            }) => self.root(callee),
            Expr::Seq(seq) => match seq.exprs.last() {
                Some(last) => self.root(last),
                None => Root::Other,
            },
            Expr::OptChain(chain) => match &*chain.base {
                OptChainBase::Member(member) => self.root(&member.obj),
                OptChainBase::Call(call) => self.root(&call.callee),
            },
            Expr::Ident(ident) if ident.ctxt == self.unresolved => Root::Global,
            Expr::Ident(ident) if self.prepass.global_aliases.contains(&ident.to_id()) => {
                Root::Global
            }
            Expr::Ident(ident) => {
                let var = self.var(ident);
                match self.m.vars[var as usize].kind {
                    VarKind::Import => Root::Import,
                    VarKind::Local => Root::Other,
                }
            }
            _ => Root::Other,
        }
    }

    fn member(&mut self, member: &MemberExpr) -> Node {
        let base = self.expr(&member.obj);
        let (prop, _) = self.member_prop(&member.prop);
        let root = self.root(&member.obj);
        self.load(base, prop, root, member.span.lo.0)
    }

    fn args(&mut self, args: &[ExprOrSpread]) -> Vec<Arg> {
        args.iter()
            .map(|arg| {
                let value = self.expr(&arg.expr);
                match arg.spread {
                    Some(_) => Arg {
                        node: self.iterate(value, arg.expr.span().lo.0),
                        spread: true,
                    },
                    None => Arg {
                        node: value,
                        spread: false,
                    },
                }
            })
            .collect()
    }

    /// A call's callee and receiver.
    fn callee(&mut self, callee: &Expr) -> (Node, Option<Node>, Root) {
        let root = self.root(callee);
        match strip_parens(callee) {
            Expr::Member(member) => {
                let base = self.expr(&member.obj);
                let (prop, _) = self.member_prop(&member.prop);
                let function = self.load(base, prop, root, member.span.lo.0);
                (function, Some(base), root)
            }
            Expr::OptChain(chain) => match &*chain.base {
                OptChainBase::Member(member) => {
                    let base = self.expr(&member.obj);
                    let (prop, _) = self.member_prop(&member.prop);
                    let function = self.load(base, prop, root, member.span.lo.0);
                    (function, Some(base), root)
                }
                OptChainBase::Call(_) => (self.expr(callee), None, root),
            },
            Expr::SuperProp(super_prop) => {
                let home = self.super_home();
                let (prop, _) = self.super_prop(&super_prop.prop);
                let function = self.load(home, prop, Root::Other, super_prop.span.lo.0);
                (function, Some(self.ctx.this), root)
            }
            Expr::Ident(ident) if ident.ctxt == self.unresolved && &*ident.sym == "eval" => {
                self.havoc();
                (ESC_NODE, None, root)
            }
            other => (self.expr(other), None, root),
        }
    }

    fn call(&mut self, callee: &Expr, args: &[ExprOrSpread], span_lo: u32) -> Node {
        if let Some(result) = self.known_call(callee, args, span_lo) {
            return result;
        }
        let (callee, this, root) = self.callee(callee);
        let args = self.args(args);
        let dst = self.node();
        self.cx(
            Cx::Call {
                callee,
                this,
                args,
                dst,
                new_site: None,
                root,
                coercion: false,
            },
            span_lo,
        );
        dst
    }

    fn expr(&mut self, expr: &Expr) -> Node {
        let span_lo = expr.span().lo.0;
        match expr {
            Expr::This(_) => self.ctx.this,
            Expr::Array(array) => {
                let literal_children = array
                    .elems
                    .iter()
                    .flatten()
                    .filter(|element| element.spread.is_none() && is_literal(&element.expr))
                    .count();
                let (obj, _, summarize) = self.literal(true, literal_children, span_lo);
                for element in array.elems.iter().flatten() {
                    let summarize = summarize && element.spread.is_none();
                    let mut value = self.literal_child(obj, summarize, &element.expr);
                    if element.spread.is_some() {
                        value = self.iterate(value, span_lo);
                    }
                    self.m.store_value(obj, INDEX, value);
                }
                self.m.obj_node(obj)
            }
            Expr::Object(object) => self.object(object, span_lo),
            Expr::Fn(function) => self.fn_expr(function.ident.as_ref(), &function.function),
            Expr::Arrow(arrow) => {
                let body = match &*arrow.body {
                    ArrowFunctionBody::FunctionBody(block) => Body::Block(&block.stmts),
                    ArrowFunctionBody::Expr(expr) => Body::Expr(expr),
                };
                let obj = self.function(FnShape {
                    params: arrow.params.iter().collect(),
                    body,
                    arrow: true,
                    is_async: arrow.is_async,
                    is_generator: arrow.is_generator,
                    span_lo,
                    super_home: None,
                });
                self.m.obj_node(obj)
            }
            Expr::Class(class) => self.class_expr(class.ident.as_ref(), &class.class),
            Expr::Unary(unary) => {
                if unary.op == UnaryOp::Delete {
                    self.delete(&unary.arg, span_lo);
                    return EMPTY_NODE;
                }
                let value = self.expr(&unary.arg);
                if matches!(unary.op, UnaryOp::Minus | UnaryOp::Plus | UnaryOp::Tilde) {
                    self.coerce(value, span_lo);
                }
                EMPTY_NODE
            }
            Expr::Update(update) => {
                let current = self.expr(&update.arg);
                self.coerce(current, span_lo);
                if matches!(strip_parens(&update.arg), Expr::Ident(_)) {
                    self.numeric_hint = Some(Vec::new());
                }
                self.assign_expr_target(&update.arg, EMPTY_NODE, span_lo);
                EMPTY_NODE
            }
            Expr::Bin(binary) => self.binary(binary, span_lo),
            Expr::Assign(assign) => self.assign(assign, span_lo),
            Expr::Member(member) => self.member(member),
            Expr::SuperProp(super_prop) => {
                let home = self.super_home();
                let (prop, _) = self.super_prop(&super_prop.prop);
                self.load(home, prop, Root::Other, span_lo)
            }
            Expr::Cond(cond) => {
                self.expr(&cond.test);
                let cons = self.expr(&cond.cons);
                let alt = self.expr(&cond.alt);
                self.union(&[cons, alt])
            }
            Expr::Call(call) => match &call.callee {
                Callee::Expr(callee) => self.call(callee, &call.args, span_lo),
                Callee::Super(_) => {
                    let callee = match self.ctx.super_class {
                        Some(class) => class,
                        None => {
                            self.havoc();
                            ESC_NODE
                        }
                    };
                    let args = self.args(&call.args);
                    let dst = self.node();
                    self.cx(
                        Cx::Call {
                            callee,
                            this: Some(self.ctx.this),
                            args,
                            dst,
                            new_site: None,
                            root: Root::Other,
                            coercion: false,
                        },
                        span_lo,
                    );
                    dst
                }
                Callee::Import(_) => {
                    for arg in &call.args {
                        let value = self.expr(&arg.expr);
                        self.coerce(value, span_lo);
                    }
                    self.opaque(Reason::Suspension);
                    ESC_NODE
                }
            },
            Expr::New(new) => {
                let callee = self.expr(&new.callee);
                let root = self.root(&new.callee);
                let args = self.args(new.args.as_deref().unwrap_or_default());
                let site = self.alloc(ObjKind::Plain, span_lo);
                let dst = self.node();
                self.cx(
                    Cx::Call {
                        callee,
                        this: None,
                        args,
                        dst,
                        new_site: Some(site),
                        root,
                        coercion: false,
                    },
                    span_lo,
                );
                dst
            }
            Expr::Seq(seq) => {
                let mut last = EMPTY_NODE;
                for expr in &seq.exprs {
                    last = self.expr(expr);
                }
                last
            }
            Expr::Ident(ident) => self.read_ident(ident),
            Expr::Lit(Lit::Regex(_)) => {
                let obj = self.alloc(ObjKind::Regex, span_lo);
                self.m.obj_node(obj)
            }
            Expr::Lit(Lit::JSXText(_)) => {
                self.havoc();
                ESC_NODE
            }
            Expr::Lit(_) => EMPTY_NODE,
            Expr::Tpl(template) => {
                for expr in &template.exprs {
                    let value = self.expr(expr);
                    self.coerce(value, span_lo);
                }
                EMPTY_NODE
            }
            Expr::TaggedTpl(tagged) => {
                let (callee, this, root) = self.callee(&tagged.tag);
                let strings = self.alloc(ObjKind::Array, span_lo);
                let mut args = vec![Arg {
                    node: self.m.obj_node(strings),
                    spread: false,
                }];
                for expr in &tagged.tpl.exprs {
                    let node = self.expr(expr);
                    args.push(Arg {
                        node,
                        spread: false,
                    });
                }
                let dst = self.node();
                self.cx(
                    Cx::Call {
                        callee,
                        this,
                        args,
                        dst,
                        new_site: None,
                        root,
                        coercion: false,
                    },
                    span_lo,
                );
                dst
            }
            Expr::Yield(yielded) => {
                if let Some(arg) = &yielded.arg {
                    let value = self.expr(arg);
                    self.escape(value);
                }
                self.opaque(Reason::Suspension);
                ESC_NODE
            }
            Expr::Await(awaited) => {
                let value = self.expr(&awaited.arg);
                // Promise resolution reads `then` now and calls it later.
                let then = self.m.props.intern("then");
                self.load(value, then, Root::Other, span_lo);
                self.escape(value);
                if self.ctx.top {
                    self.havoc();
                }
                self.opaque(Reason::Suspension);
                ESC_NODE
            }
            Expr::MetaProp(meta) => match meta.kind {
                MetaPropKind::ImportMeta => ESC_NODE,
                MetaPropKind::NewTarget => {
                    self.havoc();
                    ESC_NODE
                }
            },
            Expr::Paren(paren) => self.expr(&paren.expr),
            Expr::OptChain(chain) => match &*chain.base {
                OptChainBase::Member(member) => self.member(member),
                OptChainBase::Call(call) => self.call(&call.callee, &call.args, span_lo),
            },
            Expr::PrivateName(_) => EMPTY_NODE,
            _ => {
                self.havoc();
                ESC_NODE
            }
        }
    }

    fn delete(&mut self, target: &Expr, span_lo: u32) {
        match strip_parens(target) {
            Expr::Member(member) => {
                let base = self.expr(&member.obj);
                let (prop, _) = self.member_prop(&member.prop);
                self.key_deps = None;
                match prop {
                    ANY => self.m.delete_any = true,
                    INDEX => {}
                    _ => {
                        self.m.deleted.insert(prop);
                    }
                }
                self.cx(
                    Cx::Store {
                        base,
                        prop,
                        src: EMPTY_NODE,
                        global_rooted: false,
                    },
                    span_lo,
                );
            }
            Expr::OptChain(_) | Expr::SuperProp(_) => self.havoc(),
            other => {
                self.expr(other);
            }
        }
    }

    fn binary(&mut self, binary: &BinExpr, span_lo: u32) -> Node {
        let left = self.expr(&binary.left);
        let right = self.expr(&binary.right);
        match binary.op {
            BinaryOp::LogicalOr | BinaryOp::LogicalAnd | BinaryOp::NullishCoalescing => {
                self.union(&[left, right])
            }
            BinaryOp::EqEqEq | BinaryOp::NotEqEq => EMPTY_NODE,
            BinaryOp::In => {
                self.coerce(left, span_lo);
                self.cx(Cx::Touch { val: right }, span_lo);
                EMPTY_NODE
            }
            BinaryOp::InstanceOf => {
                // `Symbol.hasInstance` on the right, a Proxy on the left.
                self.cx(Cx::Touch { val: left }, span_lo);
                let has_instance = self.m.props.intern("@@hasInstance");
                let callee = self.load(right, has_instance, Root::Other, span_lo);
                let dst = self.node();
                self.cx(
                    Cx::Call {
                        callee,
                        this: Some(right),
                        args: vec![Arg {
                            node: left,
                            spread: false,
                        }],
                        dst,
                        new_site: None,
                        root: Root::Other,
                        coercion: false,
                    },
                    span_lo,
                );
                let prototype = self.m.props.intern("prototype");
                self.load(right, prototype, Root::Other, span_lo);
                EMPTY_NODE
            }
            _ => {
                self.coerce(left, span_lo);
                self.coerce(right, span_lo);
                EMPTY_NODE
            }
        }
    }

    fn assign(&mut self, assign: &AssignExpr, span_lo: u32) -> Node {
        let to_ident = matches!(
            assign.left,
            AssignTarget::Simple(SimpleAssignTarget::Ident(_))
        );
        if assign.op == AssignOp::Assign {
            let value = self.expr(&assign.right);
            if to_ident {
                self.numeric_hint = self.numeric(&assign.right);
            }
            self.assign_target(&assign.left, value, span_lo);
            return value;
        }
        let current = self.read_target(&assign.left, span_lo);
        let right = self.expr(&assign.right);
        match assign.op {
            AssignOp::AndAssign | AssignOp::OrAssign | AssignOp::NullishAssign => {
                self.assign_target(&assign.left, right, span_lo);
                self.union(&[current, right])
            }
            _ => {
                self.coerce(current, span_lo);
                self.coerce(right, span_lo);
                if to_ident && assign.op != AssignOp::AddAssign {
                    self.numeric_hint = Some(Vec::new());
                }
                self.assign_target(&assign.left, EMPTY_NODE, span_lo);
                EMPTY_NODE
            }
        }
    }

    fn read_target(&mut self, target: &AssignTarget, span_lo: u32) -> Node {
        match target {
            AssignTarget::Simple(SimpleAssignTarget::Ident(binding)) => {
                self.read_ident(&binding.id)
            }
            AssignTarget::Simple(SimpleAssignTarget::Member(member)) => self.member(member),
            AssignTarget::Simple(SimpleAssignTarget::SuperProp(super_prop)) => {
                let home = self.super_home();
                let (prop, _) = self.super_prop(&super_prop.prop);
                self.load(home, prop, Root::Other, span_lo)
            }
            AssignTarget::Simple(SimpleAssignTarget::Paren(paren)) => self.expr(&paren.expr),
            _ => {
                self.havoc();
                ESC_NODE
            }
        }
    }

    fn assign_target(&mut self, target: &AssignTarget, value: Node, span_lo: u32) {
        match target {
            AssignTarget::Simple(simple) => match simple {
                SimpleAssignTarget::Ident(binding) => self.write_ident(&binding.id, value, false),
                SimpleAssignTarget::Member(member) => self.store_member(member, value),
                SimpleAssignTarget::SuperProp(super_prop) => {
                    let (prop, key) = self.super_prop(&super_prop.prop);
                    self.store(self.ctx.this, prop, key, value, span_lo);
                }
                SimpleAssignTarget::Paren(paren) => {
                    self.assign_expr_target(&paren.expr, value, span_lo);
                }
                _ => self.havoc(),
            },
            AssignTarget::Pat(AssignTargetPat::Array(array)) => {
                self.bind_array(array, value, false);
            }
            AssignTarget::Pat(AssignTargetPat::Object(object)) => {
                self.bind_object(object, value, false);
            }
            AssignTarget::Pat(AssignTargetPat::Invalid(_)) => self.havoc(),
        }
    }

    fn store_member(&mut self, member: &MemberExpr, value: Node) {
        let base = self.expr(&member.obj);
        let (prop, key) = self.member_prop(&member.prop);
        self.store_global_rooted = self.root(&member.obj) == Root::Global;
        self.store(base, prop, key, value, member.span.lo.0);
    }

    /// Assigns to an expression in target position.
    fn assign_expr_target(&mut self, target: &Expr, value: Node, span_lo: u32) {
        match strip_parens(target) {
            Expr::Ident(ident) => self.write_ident(ident, value, false),
            Expr::Member(member) => self.store_member(member, value),
            Expr::SuperProp(super_prop) => {
                let (prop, key) = self.super_prop(&super_prop.prop);
                self.store(self.ctx.this, prop, key, value, span_lo);
            }
            _ => self.havoc(),
        }
    }

    fn bind(&mut self, pat: &Pat, value: Node, declaration: bool) {
        match pat {
            Pat::Ident(binding) => self.write_ident(&binding.id, value, declaration),
            Pat::Array(array) => self.bind_array(array, value, declaration),
            Pat::Object(object) => self.bind_object(object, value, declaration),
            Pat::Assign(assign) => {
                let default = self.expr(&assign.right);
                let merged = self.union(&[value, default]);
                self.bind(&assign.left, merged, declaration);
            }
            Pat::Rest(rest) => {
                // Only reached for a malformed tree; rests are bound by
                // their array, object or parameter list.
                self.bind(&rest.arg, value, declaration);
                self.havoc();
            }
            Pat::Expr(expr) => self.assign_expr_target(expr, value, expr.span().lo.0),
            Pat::Invalid(_) => self.havoc(),
        }
    }

    fn bind_array(&mut self, array: &ArrayPat, value: Node, declaration: bool) {
        let span_lo = array.span.lo.0;
        let elements = self.iterate(value, span_lo);
        for element in array.elems.iter().flatten() {
            match element {
                Pat::Rest(rest) => {
                    let obj = self.alloc(ObjKind::Array, span_lo);
                    self.m.store_value(obj, INDEX, elements);
                    let node = self.m.obj_node(obj);
                    self.bind(&rest.arg, node, declaration);
                }
                other => self.bind(other, elements, declaration),
            }
        }
    }

    fn bind_object(&mut self, object: &ObjectPat, value: Node, declaration: bool) {
        let span_lo = object.span.lo.0;
        for prop in &object.props {
            match prop {
                ObjectPatProp::KeyValue(key_value) => {
                    let (name, _) = self.prop_name(&key_value.key);
                    let loaded = self.load(value, name, Root::Other, span_lo);
                    self.bind(&key_value.value, loaded, declaration);
                }
                ObjectPatProp::Assign(assign) => {
                    let name = self.m.props.intern(&assign.key.sym);
                    let mut loaded = self.load(value, name, Root::Other, span_lo);
                    if let Some(default) = &assign.value {
                        let default = self.expr(default);
                        loaded = self.union(&[loaded, default]);
                    }
                    self.write_ident(&assign.key.id, loaded, declaration);
                }
                ObjectPatProp::Rest(rest) => {
                    let obj = self.alloc(ObjKind::Plain, span_lo);
                    let loaded = self.load(value, ANY, Root::Other, span_lo);
                    self.m.store_value(obj, ANY, loaded);
                    let node = self.m.obj_node(obj);
                    self.bind(&rest.arg, node, declaration);
                }
            }
        }
    }

    /// The pseudo-property the accessors of `name` are stored under.
    fn accessor_prop(&mut self, name: PropId, getter: bool) -> PropId {
        let kind = if getter { "get" } else { "set" };
        let pseudo = format!("\0{kind}:{}", self.m.props.name(name));
        let prop = self.m.props.intern(&pseudo);
        self.m.pseudo.insert(prop);
        prop
    }

    /// Stores an accessor function on `target`.
    fn define_accessor(&mut self, target: Obj, name: PropId, getter: bool, function: Node) {
        self.key_deps = None;
        let (names, all) = if getter {
            (&mut self.m.getter_names, GETTERS)
        } else {
            (&mut self.m.setter_names, SETTERS)
        };
        names.insert(name);
        self.m.store_value(target, all, function);
        if name != ANY && name != INDEX {
            let prop = self.accessor_prop(name, getter);
            self.m.store_value(target, prop, function);
        }
    }

    fn define(&mut self, target: Obj, name: PropId, key: Option<Node>, value: Node, span_lo: u32) {
        self.key_deps = None;
        if self.m.props.name(name) == "__proto__" {
            self.havoc();
        }
        self.m.store_value(target, name, value);
        if name != ANY && name != INDEX {
            self.m.objs[target as usize].born.insert(name);
        }
        if let Some(key) = key {
            let node = self.m.obj_node(target);
            self.exotic_when_object_key(key, node, span_lo);
        }
    }

    fn object(&mut self, object: &ObjectLit, span_lo: u32) -> Node {
        let literal_children = object
            .props
            .iter()
            .filter(|prop| {
                matches!(prop, PropOrSpread::Prop(prop)
                    if matches!(&**prop, Prop::KeyValue(key_value) if is_literal(&key_value.value)))
            })
            .count();
        let (obj, earlier_born, summarize) = self.literal(false, literal_children, span_lo);
        let node = self.m.obj_node(obj);
        for prop in &object.props {
            match prop {
                PropOrSpread::Spread(spread) => {
                    let source = self.expr(&spread.expr);
                    let copied = self.load(source, ANY, Root::Other, span_lo);
                    self.m.store_value(obj, ANY, copied);
                }
                PropOrSpread::Prop(prop) => match &**prop {
                    Prop::Shorthand(ident) => {
                        let value = self.read_ident(ident);
                        let name = self.m.props.intern(&ident.sym);
                        self.define(obj, name, None, value, span_lo);
                    }
                    Prop::KeyValue(key_value) => {
                        let (name, key) = self.prop_name(&key_value.key);
                        let value = self.literal_child(obj, summarize, &key_value.value);
                        self.define(obj, name, key, value, span_lo);
                    }
                    Prop::Method(method) => {
                        let (name, key) = self.prop_name(&method.key);
                        let value = self.function_value(&method.function, None);
                        self.define(obj, name, key, value, span_lo);
                    }
                    Prop::Getter(getter) => {
                        let (name, key) = self.prop_name(&getter.key);
                        let function = self.function_value(&getter.function, None);
                        self.define_accessor(obj, name, true, function);
                        if let Some(key) = key {
                            self.exotic_when_object_key(key, node, span_lo);
                        }
                    }
                    Prop::Setter(setter) => {
                        let (name, key) = self.prop_name(&setter.key);
                        let function = self.function_value(&setter.function, None);
                        self.define_accessor(obj, name, false, function);
                        if let Some(key) = key {
                            self.exotic_when_object_key(key, node, span_lo);
                        }
                    }
                    Prop::Assign(_) => self.havoc(),
                },
            }
        }
        if let Some(earlier_born) = earlier_born {
            // Born for the summary only if born in every literal it stands for.
            self.m.objs[obj as usize]
                .born
                .retain(|prop| earlier_born.contains(prop));
        }
        node
    }

    fn fn_expr(&mut self, ident: Option<&Ident>, function: &Function) -> Node {
        let value = self.function_value(function, None);
        if let Some(ident) = ident {
            let var = self.var(ident);
            let node = self.m.vars[var as usize].node;
            self.m.edge(value, node);
        }
        value
    }

    fn function_value(&mut self, function: &Function, super_home: Option<Node>) -> Node {
        let obj = self.function(FnShape {
            params: function.params.iter().map(|param| &param.pat).collect(),
            body: match &function.body {
                Some(body) => Body::Block(&body.stmts),
                None => Body::Empty,
            },
            arrow: false,
            is_async: function.is_async,
            is_generator: function.is_generator,
            span_lo: function.span.lo.0,
            super_home,
        });
        self.m.obj_node(obj)
    }

    fn new_function(&mut self, arrow: bool, span_lo: u32, param_count: usize) -> (Obj, u32) {
        let index = self.m.fns.len() as u32;
        let obj = self.alloc(ObjKind::Function(index), span_lo);
        let unit = self.m.units.len() as Unit;
        self.m.units.push(UnitInfo {
            function: Some(obj),
            ..UnitInfo::default()
        });
        let params = (0..param_count).map(|_| self.node()).collect();
        let all_args = self.node();
        let this = (!arrow).then(|| self.node());
        let result = self.node();
        self.m.fns.push(FnInfo {
            obj,
            unit,
            params,
            all_args,
            this,
            result,
            has_prototype: !arrow,
            prototype: None,
        });
        for name in ["length", "name"] {
            let prop = self.m.props.intern(name);
            self.m.objs[obj as usize].born.insert(prop);
        }
        (obj, index)
    }

    fn function(&mut self, shape: FnShape<'_>) -> Obj {
        let (obj, index) = self.new_function(shape.arrow, shape.span_lo, shape.params.len());
        let info = &self.m.fns[index as usize];
        let (unit, params, all_args, own_this, ret) = (
            info.unit,
            info.params.clone(),
            info.all_args,
            info.this,
            info.result,
        );
        let suspendable = shape.is_async || shape.is_generator;
        if suspendable {
            // The caller receives a promise or generator object, and the
            // body's later part runs when unknown code resumes it.
            self.m.fns[index as usize].result = ESC_NODE;
            self.escape(ret);
            if shape.is_generator {
                self.m.add(ESCAPED_SINK, obj);
            }
        }
        let outer = self.ctx.clone();
        self.ctx = Ctx {
            unit,
            this: own_this.unwrap_or(outer.this),
            ret: Some(ret),
            args_fn: if shape.arrow {
                outer.args_fn
            } else {
                Some(index)
            },
            super_home: if shape.arrow {
                outer.super_home
            } else {
                shape.super_home
            },
            super_class: if shape.arrow { outer.super_class } else { None },
            top: false,
            suspendable,
        };
        self.bind_params(&shape.params, &params, all_args, shape.span_lo);
        match shape.body {
            Body::Block(stmts) => self.stmts(stmts),
            Body::Expr(expr) => {
                let value = self.expr(expr);
                self.m.edge(value, ret);
            }
            Body::Empty => {}
        }
        self.ctx = outer;
        obj
    }

    fn bind_params(&mut self, pats: &[&Pat], params: &[Node], all_args: Node, span_lo: u32) {
        for (pat, param) in pats.iter().zip(params) {
            match pat {
                Pat::Rest(rest) => {
                    let obj = self.alloc(ObjKind::Array, span_lo);
                    self.m.store_value(obj, INDEX, all_args);
                    let node = self.m.obj_node(obj);
                    self.bind(&rest.arg, node, true);
                }
                other => self.bind(other, *param, true),
            }
        }
    }

    fn class_expr(&mut self, ident: Option<&Ident>, class: &Class) -> Node {
        let value = self.class(class);
        if let Some(ident) = ident {
            let var = self.var(ident);
            let node = self.m.vars[var as usize].node;
            self.m.edge(value, node);
        }
        value
    }

    fn class(&mut self, class: &Class) -> Node {
        let span_lo = class.span.lo.0;
        let constructor = class.body.iter().find_map(|member| match member {
            ClassMember::Constructor(constructor) => Some(constructor),
            _ => None,
        });
        let param_count = constructor.map_or(0, |constructor| constructor.params.len());
        let (class_obj, index) = self.new_function(false, span_lo, param_count);
        let class_node = self.m.obj_node(class_obj);
        let prototype = self
            .m
            .function_prototype(class_obj)
            .expect("a class has a prototype");
        let info = &self.m.fns[index as usize];
        let (unit, params, all_args, this, ret) = (
            info.unit,
            info.params.clone(),
            info.all_args,
            info.this.expect("a class constructor binds this"),
            info.result,
        );
        let (super_class, instance_home) = match &class.super_class {
            Some(super_class) => {
                let parent = self.expr(super_class);
                let prototype_prop = self.m.props.intern("prototype");
                let parent_prototype = self.load(parent, prototype_prop, Root::Other, span_lo);
                let proto = self.m.objs[prototype as usize].proto;
                self.m.edge(parent_prototype, proto);
                let proto = self.m.objs[class_obj as usize].proto;
                self.m.edge(parent, proto);
                (Some(parent), Some(parent_prototype))
            }
            None => (None, None),
        };
        let outer = self.ctx.clone();
        let constructor_ctx = Ctx {
            unit,
            this,
            ret: Some(ret),
            args_fn: Some(index),
            super_home: instance_home,
            super_class,
            top: false,
            suspendable: false,
        };
        // Static members evaluate in the enclosing unit with the class as
        // `this`.
        let static_ctx = Ctx {
            this: class_node,
            super_home: super_class,
            super_class: None,
            ret: None,
            ..outer.clone()
        };
        match constructor {
            Some(constructor) => {
                self.ctx = constructor_ctx.clone();
                let mut pats = Vec::new();
                for param in &constructor.params {
                    match param {
                        ParamOrTsParamProp::Param(param) => pats.push(&param.pat),
                        ParamOrTsParamProp::TsParamProp(_) => self.havoc(),
                    }
                }
                self.bind_params(&pats, &params, all_args, span_lo);
                if let Some(body) = &constructor.body {
                    self.stmts(&body.stmts);
                }
            }
            None => {
                if let Some(parent) = super_class {
                    self.ctx = constructor_ctx.clone();
                    let dst = self.node();
                    self.cx(
                        Cx::Call {
                            callee: parent,
                            this: Some(this),
                            args: vec![Arg {
                                node: all_args,
                                spread: true,
                            }],
                            dst,
                            new_site: None,
                            root: Root::Other,
                            coercion: false,
                        },
                        span_lo,
                    );
                }
            }
        }
        for member in &class.body {
            self.ctx = outer.clone();
            match member {
                ClassMember::Constructor(_) | ClassMember::Empty(_) => {}
                ClassMember::Method(method) => {
                    let (name, key) = self.prop_name(&method.key);
                    self.class_method(
                        (class_obj, prototype),
                        (name, key),
                        method.kind,
                        method.is_static,
                        &method.function,
                        (super_class, instance_home),
                    );
                }
                ClassMember::PrivateMethod(method) => {
                    let name = self.private_name(&method.key);
                    self.class_method(
                        (class_obj, prototype),
                        (name, None),
                        method.kind,
                        method.is_static,
                        &method.function,
                        (super_class, instance_home),
                    );
                }
                ClassMember::ClassProp(prop) => {
                    let (name, key) = self.prop_name(&prop.key);
                    self.class_field(
                        (name, key),
                        prop.value.as_deref(),
                        prop.is_static,
                        (&static_ctx, &constructor_ctx),
                        prop.span.lo.0,
                    );
                }
                ClassMember::PrivateProp(prop) => {
                    let name = self.private_name(&prop.key);
                    self.class_field(
                        (name, None),
                        prop.value.as_deref(),
                        prop.is_static,
                        (&static_ctx, &constructor_ctx),
                        prop.span.lo.0,
                    );
                }
                ClassMember::StaticBlock(block) => {
                    self.ctx = static_ctx.clone();
                    self.stmts(&block.body.stmts);
                }
                _ => self.havoc(),
            }
        }
        self.ctx = outer;
        class_node
    }

    fn class_method(
        &mut self,
        (class_obj, prototype): (Obj, Obj),
        (name, key): (PropId, Option<Node>),
        kind: MethodKind,
        is_static: bool,
        function: &Function,
        (super_class, instance_home): (Option<Node>, Option<Node>),
    ) {
        let target = if is_static { class_obj } else { prototype };
        let home = if is_static {
            super_class
        } else {
            instance_home
        };
        let span_lo = function.span.lo.0;
        let value = self.function_value(function, home);
        match kind {
            MethodKind::Method => self.define(target, name, key, value, span_lo),
            MethodKind::Getter | MethodKind::Setter => {
                self.define_accessor(target, name, kind == MethodKind::Getter, value);
                if let Some(key) = key {
                    let node = self.m.obj_node(target);
                    self.exotic_when_object_key(key, node, span_lo);
                }
            }
        }
    }

    fn class_field(
        &mut self,
        (name, key): (PropId, Option<Node>),
        value: Option<&Expr>,
        is_static: bool,
        (static_ctx, constructor_ctx): (&Ctx, &Ctx),
        span_lo: u32,
    ) {
        self.ctx = if is_static {
            static_ctx.clone()
        } else {
            constructor_ctx.clone()
        };
        let value = match value {
            Some(value) => self.expr(value),
            None => EMPTY_NODE,
        };
        self.store(self.ctx.this, name, key, value, span_lo);
    }

    /// The pseudo-property whose value is the accessors a read or write of
    /// `prop` may run, if the chunk defines any.
    fn accessors_for(&mut self, prop: PropId, getter: bool) -> Option<PropId> {
        let names = if getter {
            &self.m.getter_names
        } else {
            &self.m.setter_names
        };
        let all = if getter { GETTERS } else { SETTERS };
        if self.m.pseudo.contains(&prop) || names.is_empty() {
            None
        } else if prop == ANY || prop == INDEX {
            // An unknown key may name any accessor; a numeric key only one
            // defined under a numeric or unknown key.
            (prop == ANY || names.contains(&ANY) || names.contains(&INDEX)).then_some(all)
        } else if names.contains(&ANY) {
            Some(all)
        } else if names.contains(&prop) {
            Some(self.accessor_prop(prop, getter))
        } else {
            None
        }
    }

    /// Adds, for each read or write of a property that may have an accessor,
    /// the call of the accessors reachable from the base.
    fn expand_accessors(&mut self) {
        for id in 0..self.m.cxs.len() {
            let (unit, span_lo) = (self.m.cxs[id].unit, self.m.cxs[id].span_lo);
            self.ctx.unit = unit;
            match self.m.cxs[id].cx.clone() {
                Cx::Load {
                    base,
                    prop,
                    dst,
                    root,
                } => {
                    let Some(accessors) = self.accessors_for(prop, true) else {
                        continue;
                    };
                    let callee = self.load(base, accessors, root, span_lo);
                    self.cx(
                        Cx::Call {
                            callee,
                            this: Some(base),
                            args: Vec::new(),
                            dst,
                            new_site: None,
                            root,
                            coercion: false,
                        },
                        span_lo,
                    );
                }
                Cx::Store {
                    base, prop, src, ..
                } => {
                    let Some(accessors) = self.accessors_for(prop, false) else {
                        continue;
                    };
                    let callee = self.load(base, accessors, Root::Other, span_lo);
                    let dst = self.node();
                    self.cx(
                        Cx::Call {
                            callee,
                            this: Some(base),
                            args: vec![Arg {
                                node: src,
                                spread: false,
                            }],
                            dst,
                            new_site: None,
                            root: Root::Other,
                            coercion: false,
                        },
                        span_lo,
                    );
                }
                _ => {}
            }
        }
    }
}
