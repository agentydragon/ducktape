//! P1 of the Datalog selector resolver (`plans/selector_constraint_model.md`):
//! a faithful, **fail-closed** projection of a parsed chunk into AST facts.
//!
//! Fail-closed by construction: the walk's only catch-all is a loud
//! [`Unsupported`] error, so a node type it has not modeled **crashes** rather
//! than projecting a silently-incomplete fact set — which is what would let a
//! lowered selector query under-constrain and match the wrong owner. There is
//! deliberately no `swc_ecma_visit::Visit` here: its default no-op methods make
//! a forgotten node type a silent skip, the exact failure the goal forbids.
//!
//! The matcher (`source_match`) is the fidelity source of truth for which
//! children matter and in what order; this extractor mirrors those structural
//! decisions, and the corpus differential is what proves the mirror is faithful.
//! Coverage grew construct by construct until the corpus extracts with zero
//! `Unsupported`: it now projects **100%** of the top-level statements of every
//! measured `tana/re` chunk (index, ReactGraph, Calendar, VoiceChatModal —
//! declarations, function/class bodies, control flow, calls/members, objects,
//! assignments, operators, destructuring patterns, templates, module
//! imports/exports). Any not-yet-modeled construct (e.g. TS-only nodes) still
//! errors loudly rather than projecting silently-incomplete facts.

use std::collections::{BTreeMap, HashMap};

use js_ast::statement_ordinal_for_body_index;
use serde::{Deserialize, Serialize};
use swc_ecma_ast::{
    ArrayPat, AssignTarget, AssignTargetPat, BlockStmt, BlockStmtOrExpr, Callee, Class,
    ClassMember, Decl, DefaultDecl, Expr, ExprOrSpread, ForHead, Function, ImportSpecifier, Lit,
    MemberExpr, MemberProp, MetaPropKind, Module, ModuleDecl, ModuleItem, ObjectPat, ObjectPatProp,
    OptChainBase, ParamOrTsParamProp, Pat, PrivateName, Prop, PropName, PropOrSpread,
    SimpleAssignTarget, Stmt, SuperProp, Tpl, UsingDecl, VarDecl, VarDeclOrExpr, VarDeclarator,
};

pub type NodeId = u32;

/// The syntactic kind tag of a projected node — a closed enum over every
/// construct the extractor models. The matcher joins needle and subject node
/// kinds for structural equality (`source_match`), so a node type the extractor
/// does not model never reaches here: unmodeled constructs raise [`Unsupported`]
/// up front (see the module docstring), so this enum is exhaustive by
/// construction rather than carrying a catch-all.
///
/// Each variant's PascalCase serde spelling is byte-identical to the kind tag the
/// extractor previously emitted as a `&'static str`. `as_tag` exposes that
/// spelling for the string-keyed views (`Index`, root-kind prefilters) the
/// downstream resolver and near-miss diagnostics still read.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "PascalCase")]
pub enum NodeKind {
    Array,
    ArrayPat,
    Arrow,
    Assign,
    AssignPat,
    AssignProp,
    AsyncArrow,
    AsyncFunction,
    AsyncGeneratorFunction,
    Await,
    BigIntLit,
    Bin,
    BindingIdent,
    Block,
    BoolLit,
    Break,
    Call,
    Catch,
    Class,
    ClassDecl,
    ClassExpr,
    ClassMemberEmpty,
    ClassProp,
    ComputedKey,
    Cond,
    Constructor,
    Continue,
    Debugger,
    DoWhile,
    Elision,
    Empty,
    ExportAll,
    ExportDecl,
    ExportDefault,
    ExportDefaultDecl,
    ExportNamed,
    ExprStmt,
    FnDecl,
    FnExpr,
    For,
    ForIn,
    ForOf,
    Function,
    GeneratorFunction,
    Getter,
    Ident,
    If,
    Import,
    ImportCallee,
    ImportSpecifier,
    KeyValue,
    Labeled,
    Member,
    MetaPropImportMeta,
    MetaPropNewTarget,
    Method,
    New,
    NullLit,
    NumLit,
    Object,
    ObjectMethod,
    ObjectPat,
    OptCall,
    OptChain,
    PatAssign,
    PatKeyValue,
    PropName,
    RegexLit,
    RestPat,
    Return,
    Seq,
    Setter,
    Shorthand,
    Spread,
    StaticBlock,
    StrLit,
    Super,
    SuperProp,
    Switch,
    SwitchCase,
    TaggedTpl,
    This,
    Throw,
    Tpl,
    TplQuasi,
    Try,
    Unary,
    UpdatePostfix,
    UpdatePrefix,
    VarDecl,
    VarDeclarator,
    While,
    Yield,
}

impl NodeKind {
    /// The kind tag's stable spelling, identical to the variant's PascalCase serde
    /// form. Feeds the string-keyed [`Index`] kind vector and the root-kind
    /// prefilters consumed by `source_match` (the resolver and near-miss walk).
    pub fn as_tag(self) -> &'static str {
        match self {
            Self::Array => "Array",
            Self::ArrayPat => "ArrayPat",
            Self::Arrow => "Arrow",
            Self::Assign => "Assign",
            Self::AssignPat => "AssignPat",
            Self::AssignProp => "AssignProp",
            Self::AsyncArrow => "AsyncArrow",
            Self::AsyncFunction => "AsyncFunction",
            Self::AsyncGeneratorFunction => "AsyncGeneratorFunction",
            Self::Await => "Await",
            Self::BigIntLit => "BigIntLit",
            Self::Bin => "Bin",
            Self::BindingIdent => "BindingIdent",
            Self::Block => "Block",
            Self::BoolLit => "BoolLit",
            Self::Break => "Break",
            Self::Call => "Call",
            Self::Catch => "Catch",
            Self::Class => "Class",
            Self::ClassDecl => "ClassDecl",
            Self::ClassExpr => "ClassExpr",
            Self::ClassMemberEmpty => "ClassMemberEmpty",
            Self::ClassProp => "ClassProp",
            Self::ComputedKey => "ComputedKey",
            Self::Cond => "Cond",
            Self::Constructor => "Constructor",
            Self::Continue => "Continue",
            Self::Debugger => "Debugger",
            Self::DoWhile => "DoWhile",
            Self::Elision => "Elision",
            Self::Empty => "Empty",
            Self::ExportAll => "ExportAll",
            Self::ExportDecl => "ExportDecl",
            Self::ExportDefault => "ExportDefault",
            Self::ExportDefaultDecl => "ExportDefaultDecl",
            Self::ExportNamed => "ExportNamed",
            Self::ExprStmt => "ExprStmt",
            Self::FnDecl => "FnDecl",
            Self::FnExpr => "FnExpr",
            Self::For => "For",
            Self::ForIn => "ForIn",
            Self::ForOf => "ForOf",
            Self::Function => "Function",
            Self::GeneratorFunction => "GeneratorFunction",
            Self::Getter => "Getter",
            Self::Ident => "Ident",
            Self::If => "If",
            Self::Import => "Import",
            Self::ImportCallee => "ImportCallee",
            Self::ImportSpecifier => "ImportSpecifier",
            Self::KeyValue => "KeyValue",
            Self::Labeled => "Labeled",
            Self::Member => "Member",
            Self::MetaPropImportMeta => "MetaPropImportMeta",
            Self::MetaPropNewTarget => "MetaPropNewTarget",
            Self::Method => "Method",
            Self::New => "New",
            Self::NullLit => "NullLit",
            Self::NumLit => "NumLit",
            Self::Object => "Object",
            Self::ObjectMethod => "ObjectMethod",
            Self::ObjectPat => "ObjectPat",
            Self::OptCall => "OptCall",
            Self::OptChain => "OptChain",
            Self::PatAssign => "PatAssign",
            Self::PatKeyValue => "PatKeyValue",
            Self::PropName => "PropName",
            Self::RegexLit => "RegexLit",
            Self::RestPat => "RestPat",
            Self::Return => "Return",
            Self::Seq => "Seq",
            Self::Setter => "Setter",
            Self::Shorthand => "Shorthand",
            Self::Spread => "Spread",
            Self::StaticBlock => "StaticBlock",
            Self::StrLit => "StrLit",
            Self::Super => "Super",
            Self::SuperProp => "SuperProp",
            Self::Switch => "Switch",
            Self::SwitchCase => "SwitchCase",
            Self::TaggedTpl => "TaggedTpl",
            Self::This => "This",
            Self::Throw => "Throw",
            Self::Tpl => "Tpl",
            Self::TplQuasi => "TplQuasi",
            Self::Try => "Try",
            Self::Unary => "Unary",
            Self::UpdatePostfix => "UpdatePostfix",
            Self::UpdatePrefix => "UpdatePrefix",
            Self::VarDecl => "VarDecl",
            Self::VarDeclarator => "VarDeclarator",
            Self::While => "While",
            Self::Yield => "Yield",
        }
    }
}

/// A faithful relational projection of one chunk's top-level statements. Each
/// vector is an EDB relation the lowered selector queries join over.
#[derive(Debug, Default)]
pub struct ChunkFacts {
    /// node -> syntactic kind tag.
    pub node_kind: Vec<(NodeId, NodeKind)>,
    /// parent -> (source-order ordinal, child). The ordinal is what the
    /// run-hole / adjacency encoding compares (`i < j`).
    pub child: Vec<(NodeId, u32, NodeId)>,
    /// string-literal value, unescaped via `js_ast::str_value`.
    pub str_lit: Vec<(NodeId, String)>,
    /// numeric-literal token, rendered faithfully (source `raw` when present).
    pub num_lit: Vec<(NodeId, String)>,
    pub bool_lit: Vec<(NodeId, bool)>,
    /// Identifier spelling. Exact identifier comparisons are retained as an
    /// internal lowering / solver constraint even though public `source_match`
    /// authoring uses alpha-all matching.
    pub ident_name: Vec<(NodeId, String)>,
    /// member / property / method name (non-computed).
    pub prop_name: Vec<(NodeId, String)>,
    /// distinguishing keyword/operator token: the operator for `Bin` / `Unary`
    /// / `Update` / `Assign`, and the declaration keyword (`var`/`let`/`const`)
    /// for `VarDecl` — both are exact labels the matcher compares.
    pub operator: Vec<(NodeId, String)>,
    /// regex literal -> (pattern, flags), both T-invariant labels.
    pub regex: Vec<(NodeId, String, String)>,
    /// class node -> its super-class expression node (the syntactic `extends`).
    pub super_class: Vec<(NodeId, NodeId)>,
    /// top-level statement node -> its owner statement ordinal (owner-graph join).
    pub top_level: Vec<(NodeId, usize)>,
}

/// A construct the extractor has not modeled yet. Loud by design — never a
/// silent gap. `context` names where the gap is, so coverage can grow.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Unsupported {
    pub context: &'static str,
}

fn unsupported<T>(context: &'static str) -> Result<T, Unsupported> {
    Err(Unsupported { context })
}

/// Name the unmodeled `Expr` variant so the coverage histogram ranks what to
/// implement next. Diagnostic only — fail-closed correctness is the error
/// itself, not the label precision; rare variants fall through to `expr:other`.
fn expr_variant_name(expr: &Expr) -> &'static str {
    match expr {
        Expr::Object(_) => "expr:object",
        Expr::Assign(_) => "expr:assign",
        Expr::Arrow(_) => "expr:arrow",
        Expr::Fn(_) => "expr:fn",
        Expr::Tpl(_) => "expr:tpl",
        Expr::TaggedTpl(_) => "expr:tagged_tpl",
        Expr::Await(_) => "expr:await",
        Expr::Update(_) => "expr:update",
        Expr::Yield(_) => "expr:yield",
        Expr::OptChain(_) => "expr:opt_chain",
        Expr::This(_) => "expr:this",
        Expr::MetaProp(_) => "expr:meta_prop",
        Expr::Class(_) => "expr:class",
        Expr::SuperProp(_) => "expr:super_prop",
        _ => "expr:other",
    }
}

#[derive(Default)]
struct Extractor {
    facts: ChunkFacts,
    next: NodeId,
}

impl Extractor {
    fn node(&mut self, kind: NodeKind) -> NodeId {
        let id = self.next;
        self.next += 1;
        self.facts.node_kind.push((id, kind));
        id
    }

    fn module_item(&mut self, item: &ModuleItem, ordinal: usize) -> Result<NodeId, Unsupported> {
        let id = match item {
            ModuleItem::Stmt(stmt) => self.stmt(stmt)?,
            ModuleItem::ModuleDecl(decl) => self.module_decl(decl)?,
        };
        self.facts.top_level.push((id, ordinal));
        Ok(id)
    }

    fn module_decl(&mut self, decl: &ModuleDecl) -> Result<NodeId, Unsupported> {
        match decl {
            ModuleDecl::ExportDecl(export) => {
                let id = self.node(NodeKind::ExportDecl);
                let inner = self.decl(&export.decl)?;
                self.facts.child.push((id, 0, inner));
                Ok(id)
            }
            ModuleDecl::Import(import) => {
                let id = self.node(NodeKind::Import);
                let src = self.node(NodeKind::StrLit);
                self.facts
                    .str_lit
                    .push((src, js_ast::str_value(&import.src)));
                self.facts.child.push((id, 0, src));
                for (index, spec) in import.specifiers.iter().enumerate() {
                    let local = match spec {
                        ImportSpecifier::Named(named) => &named.local,
                        ImportSpecifier::Default(default) => &default.local,
                        ImportSpecifier::Namespace(namespace) => &namespace.local,
                    };
                    let spec_id = self.node(NodeKind::ImportSpecifier);
                    self.facts.ident_name.push((spec_id, local.sym.to_string()));
                    self.facts.child.push((id, (index + 1) as u32, spec_id));
                }
                Ok(id)
            }
            ModuleDecl::ExportDefaultExpr(export) => {
                let id = self.node(NodeKind::ExportDefault);
                let expr = self.expr(&export.expr)?;
                self.facts.child.push((id, 0, expr));
                Ok(id)
            }
            ModuleDecl::ExportDefaultDecl(export) => {
                let id = self.node(NodeKind::ExportDefaultDecl);
                match &export.decl {
                    DefaultDecl::Class(class_expr) => {
                        let class = self.class_node(&class_expr.class)?;
                        self.facts.child.push((id, 0, class));
                    }
                    DefaultDecl::Fn(fn_expr) => {
                        let function = self.function(&fn_expr.function)?;
                        self.facts.child.push((id, 0, function));
                    }
                    DefaultDecl::TsInterfaceDecl(_) => {
                        return unsupported("export default: ts interface");
                    }
                }
                Ok(id)
            }
            ModuleDecl::ExportNamed(export) => {
                let id = self.node(NodeKind::ExportNamed);
                if let Some(src) = &export.src {
                    let src_id = self.node(NodeKind::StrLit);
                    self.facts.str_lit.push((src_id, js_ast::str_value(src)));
                    self.facts.child.push((id, 0, src_id));
                }
                Ok(id)
            }
            ModuleDecl::ExportAll(export) => {
                let id = self.node(NodeKind::ExportAll);
                let src = self.node(NodeKind::StrLit);
                self.facts
                    .str_lit
                    .push((src, js_ast::str_value(&export.src)));
                self.facts.child.push((id, 0, src));
                Ok(id)
            }
            _ => unsupported("module_decl"),
        }
    }

    fn stmt(&mut self, stmt: &Stmt) -> Result<NodeId, Unsupported> {
        match stmt {
            Stmt::Decl(decl) => self.decl(decl),
            Stmt::Expr(expr_stmt) => {
                let id = self.node(NodeKind::ExprStmt);
                let inner = self.expr(&expr_stmt.expr)?;
                self.facts.child.push((id, 0, inner));
                Ok(id)
            }
            Stmt::Return(ret) => {
                let id = self.node(NodeKind::Return);
                if let Some(arg) = &ret.arg {
                    let arg = self.expr(arg)?;
                    self.facts.child.push((id, 0, arg));
                }
                Ok(id)
            }
            Stmt::If(if_stmt) => {
                let id = self.node(NodeKind::If);
                let test = self.expr(&if_stmt.test)?;
                self.facts.child.push((id, 0, test));
                let cons = self.stmt(&if_stmt.cons)?;
                self.facts.child.push((id, 1, cons));
                if let Some(alt) = &if_stmt.alt {
                    let alt = self.stmt(alt)?;
                    self.facts.child.push((id, 2, alt));
                }
                Ok(id)
            }
            Stmt::While(while_stmt) => {
                let id = self.node(NodeKind::While);
                let test = self.expr(&while_stmt.test)?;
                self.facts.child.push((id, 0, test));
                let body = self.stmt(&while_stmt.body)?;
                self.facts.child.push((id, 1, body));
                Ok(id)
            }
            Stmt::DoWhile(do_while) => {
                let id = self.node(NodeKind::DoWhile);
                let body = self.stmt(&do_while.body)?;
                self.facts.child.push((id, 0, body));
                let test = self.expr(&do_while.test)?;
                self.facts.child.push((id, 1, test));
                Ok(id)
            }
            Stmt::Throw(throw) => {
                let id = self.node(NodeKind::Throw);
                let arg = self.expr(&throw.arg)?;
                self.facts.child.push((id, 0, arg));
                Ok(id)
            }
            Stmt::Block(block) => self.block(block),
            Stmt::Try(try_stmt) => {
                let id = self.node(NodeKind::Try);
                let block = self.block(&try_stmt.block)?;
                self.facts.child.push((id, 0, block));
                if let Some(handler) = &try_stmt.handler {
                    let catch = self.node(NodeKind::Catch);
                    let mut next = 0u32;
                    if let Some(param) = &handler.param {
                        let param = self.pat(param)?;
                        self.facts.child.push((catch, next, param));
                        next += 1;
                    }
                    let body = self.block(&handler.body)?;
                    self.facts.child.push((catch, next, body));
                    self.facts.child.push((id, 1, catch));
                }
                if let Some(finalizer) = &try_stmt.finalizer {
                    let finalizer = self.block(finalizer)?;
                    self.facts.child.push((id, 2, finalizer));
                }
                Ok(id)
            }
            Stmt::Switch(switch) => {
                let id = self.node(NodeKind::Switch);
                let discriminant = self.expr(&switch.discriminant)?;
                self.facts.child.push((id, 0, discriminant));
                for (index, case) in switch.cases.iter().enumerate() {
                    let case_id = self.node(NodeKind::SwitchCase);
                    let mut next = 0u32;
                    if let Some(test) = &case.test {
                        let test = self.expr(test)?;
                        self.facts.child.push((case_id, next, test));
                        next += 1;
                    }
                    for stmt in &case.cons {
                        let stmt = self.stmt(stmt)?;
                        self.facts.child.push((case_id, next, stmt));
                        next += 1;
                    }
                    self.facts.child.push((id, (index + 1) as u32, case_id));
                }
                Ok(id)
            }
            Stmt::Labeled(labeled) => {
                let id = self.node(NodeKind::Labeled);
                let body = self.stmt(&labeled.body)?;
                self.facts.child.push((id, 0, body));
                Ok(id)
            }
            // Loop/branch jumps carry only a T-variant label; the node identity
            // is what selectors anchor on.
            Stmt::Break(_) => Ok(self.node(NodeKind::Break)),
            Stmt::Continue(_) => Ok(self.node(NodeKind::Continue)),
            Stmt::Empty(_) => Ok(self.node(NodeKind::Empty)),
            Stmt::Debugger(_) => Ok(self.node(NodeKind::Debugger)),
            Stmt::For(for_stmt) => {
                let id = self.node(NodeKind::For);
                if let Some(init) = &for_stmt.init {
                    let init = self.var_decl_or_expr(init)?;
                    self.facts.child.push((id, 0, init));
                }
                if let Some(test) = &for_stmt.test {
                    let test = self.expr(test)?;
                    self.facts.child.push((id, 1, test));
                }
                if let Some(update) = &for_stmt.update {
                    let update = self.expr(update)?;
                    self.facts.child.push((id, 2, update));
                }
                let body = self.stmt(&for_stmt.body)?;
                self.facts.child.push((id, 3, body));
                Ok(id)
            }
            Stmt::ForIn(for_in) => {
                let id = self.node(NodeKind::ForIn);
                let left = self.for_head(&for_in.left)?;
                self.facts.child.push((id, 0, left));
                let right = self.expr(&for_in.right)?;
                self.facts.child.push((id, 1, right));
                let body = self.stmt(&for_in.body)?;
                self.facts.child.push((id, 2, body));
                Ok(id)
            }
            Stmt::ForOf(for_of) => {
                let id = self.node(NodeKind::ForOf);
                let left = self.for_head(&for_of.left)?;
                self.facts.child.push((id, 0, left));
                let right = self.expr(&for_of.right)?;
                self.facts.child.push((id, 1, right));
                let body = self.stmt(&for_of.body)?;
                self.facts.child.push((id, 2, body));
                Ok(id)
            }
            _ => unsupported("stmt"),
        }
    }

    fn decl(&mut self, decl: &Decl) -> Result<NodeId, Unsupported> {
        match decl {
            Decl::Var(var) => self.var_decl(var),
            Decl::Using(using) => self.using_decl(using),
            Decl::Fn(fn_decl) => {
                let id = self.node(NodeKind::FnDecl);
                let name = self.node(NodeKind::Ident);
                self.facts
                    .ident_name
                    .push((name, fn_decl.ident.sym.to_string()));
                self.facts.child.push((id, 0, name));
                let function = self.function(&fn_decl.function)?;
                self.facts.child.push((id, 1, function));
                Ok(id)
            }
            Decl::Class(class_decl) => {
                let id = self.node(NodeKind::ClassDecl);
                let name = self.node(NodeKind::Ident);
                self.facts
                    .ident_name
                    .push((name, class_decl.ident.sym.to_string()));
                self.facts.child.push((id, 0, name));
                let class = self.class_node(&class_decl.class)?;
                self.facts.child.push((id, 1, class));
                Ok(id)
            }
            _ => unsupported("decl"),
        }
    }

    fn var_decl(&mut self, var: &VarDecl) -> Result<NodeId, Unsupported> {
        let id = self.node(NodeKind::VarDecl);
        // The declaration keyword distinguishes `let`/`const`/`var` — a label
        // the matcher compares, so a `let` selector cannot match a `const`.
        self.facts
            .operator
            .push((id, js_ast::var_decl_kind_str(var.kind).to_string()));
        for (index, declarator) in var.decls.iter().enumerate() {
            let d = self.var_declarator(declarator)?;
            self.facts.child.push((id, index as u32, d));
        }
        Ok(id)
    }

    /// `using`/`await using` declarations (explicit resource management) are
    /// their own `Decl` variant in `swc_ecma_ast`, not a `VarDeclKind`, but
    /// share `VarDecl`'s shape exactly (a `Vec<VarDeclarator>`). Modeled as
    /// the same `NodeKind::VarDecl` node with an `"using"` / `"await using"`
    /// operator label, the same way `var`/`let`/`const` share one node kind
    /// and differ only by their `operator` fact.
    fn using_decl(&mut self, using: &UsingDecl) -> Result<NodeId, Unsupported> {
        let id = self.node(NodeKind::VarDecl);
        self.facts.operator.push((
            id,
            if using.is_await {
                "await using"
            } else {
                "using"
            }
            .to_string(),
        ));
        for (index, declarator) in using.decls.iter().enumerate() {
            let d = self.var_declarator(declarator)?;
            self.facts.child.push((id, index as u32, d));
        }
        Ok(id)
    }

    fn var_decl_or_expr(&mut self, init: &VarDeclOrExpr) -> Result<NodeId, Unsupported> {
        match init {
            VarDeclOrExpr::VarDecl(var) => self.var_decl(var),
            VarDeclOrExpr::Expr(expr) => self.expr(expr),
        }
    }

    fn for_head(&mut self, head: &ForHead) -> Result<NodeId, Unsupported> {
        match head {
            ForHead::VarDecl(var) => self.var_decl(var),
            ForHead::UsingDecl(using) => self.using_decl(using),
            ForHead::Pat(pat) => self.pat(pat),
        }
    }

    fn var_declarator(&mut self, declarator: &VarDeclarator) -> Result<NodeId, Unsupported> {
        let id = self.node(NodeKind::VarDeclarator);
        let name = self.pat(&declarator.name)?;
        self.facts.child.push((id, 0, name));
        if let Some(init) = &declarator.init {
            let init = self.expr(init)?;
            self.facts.child.push((id, 1, init));
        }
        Ok(id)
    }

    fn function(&mut self, function: &Function) -> Result<NodeId, Unsupported> {
        // async/generator are part of the function's identity (production compares
        // them via `eq_ignore_span`), so they must distinguish the node — fold them
        // into the kind tag the matcher compares exactly, not drop them.
        let kind = match (function.is_async, function.is_generator) {
            (false, false) => NodeKind::Function,
            (true, false) => NodeKind::AsyncFunction,
            (false, true) => NodeKind::GeneratorFunction,
            (true, true) => NodeKind::AsyncGeneratorFunction,
        };
        let id = self.node(kind);
        for (index, param) in function.params.iter().enumerate() {
            let pat = self.pat(&param.pat)?;
            self.facts.child.push((id, index as u32, pat));
        }
        if let Some(body) = &function.body {
            let block = self.block(body)?;
            self.facts
                .child
                .push((id, function.params.len() as u32, block));
        }
        Ok(id)
    }

    fn block(&mut self, block: &BlockStmt) -> Result<NodeId, Unsupported> {
        let id = self.node(NodeKind::Block);
        for (index, stmt) in block.stmts.iter().enumerate() {
            let stmt = self.stmt(stmt)?;
            self.facts.child.push((id, index as u32, stmt));
        }
        Ok(id)
    }

    fn class_node(&mut self, class: &Class) -> Result<NodeId, Unsupported> {
        let id = self.node(NodeKind::Class);
        if let Some(super_class) = &class.super_class {
            let super_node = self.expr(super_class)?;
            self.facts.super_class.push((id, super_node));
        }
        for (index, member) in class.body.iter().enumerate() {
            let member = self.class_member(member)?;
            self.facts.child.push((id, index as u32, member));
        }
        Ok(id)
    }

    fn class_member(&mut self, member: &ClassMember) -> Result<NodeId, Unsupported> {
        match member {
            ClassMember::Method(method) => {
                let id = self.node(NodeKind::Method);
                let key = self.prop_key(&method.key)?;
                self.facts.child.push((id, 0, key));
                let function = self.function(&method.function)?;
                self.facts.child.push((id, 1, function));
                Ok(id)
            }
            ClassMember::Constructor(constructor) => {
                let id = self.node(NodeKind::Constructor);
                let key = self.prop_key(&constructor.key)?;
                self.facts.child.push((id, 0, key));
                let mut next = 1u32;
                for param in &constructor.params {
                    match param {
                        ParamOrTsParamProp::Param(param) => {
                            let pat = self.pat(&param.pat)?;
                            self.facts.child.push((id, next, pat));
                            next += 1;
                        }
                        ParamOrTsParamProp::TsParamProp(_) => {
                            return unsupported("constructor: ts param prop");
                        }
                    }
                }
                if let Some(body) = &constructor.body {
                    let body = self.block(body)?;
                    self.facts.child.push((id, next, body));
                }
                Ok(id)
            }
            ClassMember::ClassProp(prop) => {
                let id = self.node(NodeKind::ClassProp);
                let key = self.prop_key(&prop.key)?;
                self.facts.child.push((id, 0, key));
                if let Some(value) = &prop.value {
                    let value = self.expr(value)?;
                    self.facts.child.push((id, 1, value));
                }
                Ok(id)
            }
            ClassMember::StaticBlock(static_block) => {
                let id = self.node(NodeKind::StaticBlock);
                let body = self.block(&static_block.body)?;
                self.facts.child.push((id, 0, body));
                Ok(id)
            }
            ClassMember::Empty(_) => Ok(self.node(NodeKind::ClassMemberEmpty)),
            ClassMember::PrivateMethod(method) => {
                let id = self.node(NodeKind::Method);
                let key = self.private_name_key(&method.key);
                self.facts.child.push((id, 0, key));
                let function = self.function(&method.function)?;
                self.facts.child.push((id, 1, function));
                Ok(id)
            }
            ClassMember::PrivateProp(prop) => {
                let id = self.node(NodeKind::ClassProp);
                let key = self.private_name_key(&prop.key);
                self.facts.child.push((id, 0, key));
                if let Some(value) = &prop.value {
                    let value = self.expr(value)?;
                    self.facts.child.push((id, 1, value));
                }
                Ok(id)
            }
            ClassMember::TsIndexSignature(_) | ClassMember::AutoAccessor(_) => {
                unsupported("class_member")
            }
        }
    }

    fn prop_key(&mut self, key: &PropName) -> Result<NodeId, Unsupported> {
        match key {
            PropName::Ident(name) => {
                let id = self.node(NodeKind::PropName);
                self.facts.prop_name.push((id, name.sym.to_string()));
                Ok(id)
            }
            PropName::Str(value) => {
                let id = self.node(NodeKind::PropName);
                self.facts.prop_name.push((id, js_ast::str_value(value)));
                Ok(id)
            }
            PropName::Num(number) => {
                let id = self.node(NodeKind::PropName);
                self.facts.prop_name.push((id, number.value.to_string()));
                Ok(id)
            }
            PropName::Computed(computed) => {
                let id = self.node(NodeKind::ComputedKey);
                let expr = self.expr(&computed.expr)?;
                self.facts.child.push((id, 0, expr));
                Ok(id)
            }
            _ => unsupported("prop_key"),
        }
    }

    /// `#name` private class member keys (`PrivateMethod`/`PrivateProp`) are
    /// not a `PropName` variant in `swc_ecma_ast` — they carry a `PrivateName`
    /// instead. Recorded into the same `prop_name` fact table as public keys,
    /// `#`-prefixed, matching the label convention already used elsewhere in
    /// this crate (`readoff_render.rs`, `selector_candidate_index.rs`,
    /// `shape_index.rs`) so a private key can never collide with a same-named
    /// public one.
    fn private_name_key(&mut self, key: &PrivateName) -> NodeId {
        let id = self.node(NodeKind::PropName);
        self.facts.prop_name.push((id, format!("#{}", key.name)));
        id
    }

    fn pat(&mut self, pat: &Pat) -> Result<NodeId, Unsupported> {
        match pat {
            Pat::Ident(binding) => {
                let id = self.node(NodeKind::BindingIdent);
                self.facts.ident_name.push((id, binding.id.sym.to_string()));
                Ok(id)
            }
            Pat::Array(array) => self.array_pat(array),
            Pat::Object(object) => self.object_pat(object),
            Pat::Rest(rest) => {
                let id = self.node(NodeKind::RestPat);
                let arg = self.pat(&rest.arg)?;
                self.facts.child.push((id, 0, arg));
                Ok(id)
            }
            Pat::Assign(assign) => {
                let id = self.node(NodeKind::AssignPat);
                let left = self.pat(&assign.left)?;
                self.facts.child.push((id, 0, left));
                let right = self.expr(&assign.right)?;
                self.facts.child.push((id, 1, right));
                Ok(id)
            }
            Pat::Expr(expr) => self.expr(expr),
            _ => unsupported("pat"),
        }
    }

    fn array_pat(&mut self, array: &ArrayPat) -> Result<NodeId, Unsupported> {
        let id = self.node(NodeKind::ArrayPat);
        for (index, elem) in array.elems.iter().enumerate() {
            match elem {
                Some(elem) => {
                    let elem = self.pat(elem)?;
                    self.facts.child.push((id, index as u32, elem));
                }
                None => {
                    let elision = self.node(NodeKind::Elision);
                    self.facts.child.push((id, index as u32, elision));
                }
            }
        }
        Ok(id)
    }

    fn object_pat(&mut self, object: &ObjectPat) -> Result<NodeId, Unsupported> {
        let id = self.node(NodeKind::ObjectPat);
        for (index, prop) in object.props.iter().enumerate() {
            let prop = self.object_pat_prop(prop)?;
            self.facts.child.push((id, index as u32, prop));
        }
        Ok(id)
    }

    fn object_pat_prop(&mut self, prop: &ObjectPatProp) -> Result<NodeId, Unsupported> {
        match prop {
            ObjectPatProp::KeyValue(key_value) => {
                let id = self.node(NodeKind::PatKeyValue);
                let key = self.prop_key(&key_value.key)?;
                self.facts.child.push((id, 0, key));
                let value = self.pat(&key_value.value)?;
                self.facts.child.push((id, 1, value));
                Ok(id)
            }
            ObjectPatProp::Assign(assign) => {
                let id = self.node(NodeKind::PatAssign);
                self.facts
                    .ident_name
                    .push((id, assign.key.id.sym.to_string()));
                if let Some(value) = &assign.value {
                    let value = self.expr(value)?;
                    self.facts.child.push((id, 0, value));
                }
                Ok(id)
            }
            ObjectPatProp::Rest(rest) => {
                let id = self.node(NodeKind::RestPat);
                let arg = self.pat(&rest.arg)?;
                self.facts.child.push((id, 0, arg));
                Ok(id)
            }
        }
    }

    fn expr(&mut self, expr: &Expr) -> Result<NodeId, Unsupported> {
        match expr {
            Expr::Ident(ident) => {
                let id = self.node(NodeKind::Ident);
                self.facts.ident_name.push((id, ident.sym.to_string()));
                Ok(id)
            }
            Expr::Lit(lit) => self.lit(lit),
            Expr::Member(member) => self.member(member),
            Expr::Call(call) => {
                let id = self.node(NodeKind::Call);
                let callee = match &call.callee {
                    Callee::Expr(callee) => self.expr(callee)?,
                    Callee::Super(_) => self.node(NodeKind::Super),
                    Callee::Import(_) => self.node(NodeKind::ImportCallee),
                };
                self.facts.child.push((id, 0, callee));
                self.push_args(id, 1, &call.args)?;
                Ok(id)
            }
            Expr::New(new) => {
                let id = self.node(NodeKind::New);
                let callee = self.expr(&new.callee)?;
                self.facts.child.push((id, 0, callee));
                if let Some(args) = &new.args {
                    self.push_args(id, 1, args)?;
                }
                Ok(id)
            }
            Expr::Bin(bin) => {
                let id = self.node(NodeKind::Bin);
                self.facts.operator.push((id, bin.op.as_str().to_string()));
                let left = self.expr(&bin.left)?;
                self.facts.child.push((id, 0, left));
                let right = self.expr(&bin.right)?;
                self.facts.child.push((id, 1, right));
                Ok(id)
            }
            Expr::Unary(unary) => {
                let id = self.node(NodeKind::Unary);
                self.facts
                    .operator
                    .push((id, unary.op.as_str().to_string()));
                let arg = self.expr(&unary.arg)?;
                self.facts.child.push((id, 0, arg));
                Ok(id)
            }
            Expr::Cond(cond) => {
                let id = self.node(NodeKind::Cond);
                let test = self.expr(&cond.test)?;
                self.facts.child.push((id, 0, test));
                let cons = self.expr(&cond.cons)?;
                self.facts.child.push((id, 1, cons));
                let alt = self.expr(&cond.alt)?;
                self.facts.child.push((id, 2, alt));
                Ok(id)
            }
            Expr::Seq(seq) => {
                let id = self.node(NodeKind::Seq);
                for (index, item) in seq.exprs.iter().enumerate() {
                    let item = self.expr(item)?;
                    self.facts.child.push((id, index as u32, item));
                }
                Ok(id)
            }
            Expr::Array(array) => {
                let id = self.node(NodeKind::Array);
                for (index, elem) in array.elems.iter().enumerate() {
                    match elem {
                        Some(elem) => {
                            let value = self.expr_or_spread(elem)?;
                            self.facts.child.push((id, index as u32, value));
                        }
                        // Elision keeps positions faithful (`[a, , b]`).
                        None => {
                            let elision = self.node(NodeKind::Elision);
                            self.facts.child.push((id, index as u32, elision));
                        }
                    }
                }
                Ok(id)
            }
            Expr::Fn(fn_expr) => {
                let id = self.node(NodeKind::FnExpr);
                if let Some(ident) = &fn_expr.ident {
                    let name = self.node(NodeKind::Ident);
                    self.facts.ident_name.push((name, ident.sym.to_string()));
                    self.facts.child.push((id, 0, name));
                }
                let function = self.function(&fn_expr.function)?;
                self.facts.child.push((id, 1, function));
                Ok(id)
            }
            Expr::Object(object) => {
                let id = self.node(NodeKind::Object);
                for (index, prop) in object.props.iter().enumerate() {
                    let prop = self.object_prop(prop)?;
                    self.facts.child.push((id, index as u32, prop));
                }
                Ok(id)
            }
            Expr::Assign(assign) => {
                let id = self.node(NodeKind::Assign);
                self.facts
                    .operator
                    .push((id, assign.op.as_str().to_string()));
                let target = self.assign_target(&assign.left)?;
                self.facts.child.push((id, 0, target));
                let right = self.expr(&assign.right)?;
                self.facts.child.push((id, 1, right));
                Ok(id)
            }
            Expr::Tpl(tpl) => self.tpl(tpl),
            Expr::TaggedTpl(tagged) => {
                let id = self.node(NodeKind::TaggedTpl);
                let tag = self.expr(&tagged.tag)?;
                self.facts.child.push((id, 0, tag));
                let tpl = self.tpl(&tagged.tpl)?;
                self.facts.child.push((id, 1, tpl));
                Ok(id)
            }
            Expr::Update(update) => {
                // Kind encodes prefix vs postfix (`++i` vs `i++`) faithfully.
                let id = self.node(if update.prefix {
                    NodeKind::UpdatePrefix
                } else {
                    NodeKind::UpdatePostfix
                });
                self.facts
                    .operator
                    .push((id, update.op.as_str().to_string()));
                let arg = self.expr(&update.arg)?;
                self.facts.child.push((id, 0, arg));
                Ok(id)
            }
            Expr::Arrow(arrow) => {
                // async is part of the arrow's identity (see `function`); fold it
                // into the kind tag rather than dropping it.
                let id = self.node(if arrow.is_async {
                    NodeKind::AsyncArrow
                } else {
                    NodeKind::Arrow
                });
                for (index, param) in arrow.params.iter().enumerate() {
                    let param = self.pat(param)?;
                    self.facts.child.push((id, index as u32, param));
                }
                let body = match &*arrow.body {
                    BlockStmtOrExpr::BlockStmt(block) => self.block(block)?,
                    BlockStmtOrExpr::Expr(expr) => self.expr(expr)?,
                };
                self.facts.child.push((id, arrow.params.len() as u32, body));
                Ok(id)
            }
            Expr::SuperProp(super_prop) => {
                let id = self.node(NodeKind::SuperProp);
                match &super_prop.prop {
                    SuperProp::Ident(name) => {
                        let prop = self.node(NodeKind::PropName);
                        self.facts.prop_name.push((prop, name.sym.to_string()));
                        self.facts.child.push((id, 0, prop));
                    }
                    SuperProp::Computed(computed) => {
                        let computed = self.expr(&computed.expr)?;
                        self.facts.child.push((id, 0, computed));
                    }
                }
                Ok(id)
            }
            Expr::Await(await_expr) => {
                let id = self.node(NodeKind::Await);
                let arg = self.expr(&await_expr.arg)?;
                self.facts.child.push((id, 0, arg));
                Ok(id)
            }
            Expr::Yield(yield_expr) => {
                let id = self.node(NodeKind::Yield);
                if let Some(arg) = &yield_expr.arg {
                    let arg = self.expr(arg)?;
                    self.facts.child.push((id, 0, arg));
                }
                Ok(id)
            }
            Expr::OptChain(opt_chain) => {
                let id = self.node(NodeKind::OptChain);
                let base = match &*opt_chain.base {
                    OptChainBase::Member(member) => self.member(member)?,
                    OptChainBase::Call(call) => {
                        let call_id = self.node(NodeKind::OptCall);
                        let callee = self.expr(&call.callee)?;
                        self.facts.child.push((call_id, 0, callee));
                        self.push_args(call_id, 1, &call.args)?;
                        call_id
                    }
                };
                self.facts.child.push((id, 0, base));
                Ok(id)
            }
            Expr::Class(class_expr) => {
                let id = self.node(NodeKind::ClassExpr);
                if let Some(ident) = &class_expr.ident {
                    let name = self.node(NodeKind::Ident);
                    self.facts.ident_name.push((name, ident.sym.to_string()));
                    self.facts.child.push((id, 0, name));
                }
                let class = self.class_node(&class_expr.class)?;
                self.facts.child.push((id, 1, class));
                Ok(id)
            }
            // Parentheses are transparent: the matcher matches modulo grouping.
            Expr::Paren(paren) => self.expr(&paren.expr),
            Expr::This(_) => Ok(self.node(NodeKind::This)),
            // `import.meta` / `new.target`: fixed meta-properties with no
            // renamable parts. The kind fully determines them (production compares
            // the whole node), so fold it into the node tag.
            Expr::MetaProp(meta) => Ok(self.node(match meta.kind {
                MetaPropKind::ImportMeta => NodeKind::MetaPropImportMeta,
                MetaPropKind::NewTarget => NodeKind::MetaPropNewTarget,
            })),
            other => unsupported(expr_variant_name(other)),
        }
    }

    /// Template literal: interleave quasis and exprs in source order
    /// (`q0 e0 q1 e1 … qn`). Shared by `Tpl` and the `tpl` of a `TaggedTpl`.
    fn tpl(&mut self, tpl: &Tpl) -> Result<NodeId, Unsupported> {
        let id = self.node(NodeKind::Tpl);
        let mut ordinal = 0u32;
        for (index, quasi) in tpl.quasis.iter().enumerate() {
            let q = self.node(NodeKind::TplQuasi);
            let value = quasi
                .cooked
                .as_ref()
                .map(|cooked| cooked.to_string_lossy().into_owned())
                .unwrap_or_else(|| quasi.raw.to_string());
            self.facts.str_lit.push((q, value));
            self.facts.child.push((id, ordinal, q));
            ordinal += 1;
            if let Some(expr) = tpl.exprs.get(index) {
                let expr = self.expr(expr)?;
                self.facts.child.push((id, ordinal, expr));
                ordinal += 1;
            }
        }
        Ok(id)
    }

    fn object_prop(&mut self, prop: &PropOrSpread) -> Result<NodeId, Unsupported> {
        match prop {
            PropOrSpread::Spread(spread) => {
                let id = self.node(NodeKind::Spread);
                let expr = self.expr(&spread.expr)?;
                self.facts.child.push((id, 0, expr));
                Ok(id)
            }
            PropOrSpread::Prop(prop) => match &**prop {
                Prop::Shorthand(ident) => {
                    let id = self.node(NodeKind::Shorthand);
                    self.facts.ident_name.push((id, ident.sym.to_string()));
                    Ok(id)
                }
                Prop::KeyValue(key_value) => {
                    let id = self.node(NodeKind::KeyValue);
                    let key = self.prop_key(&key_value.key)?;
                    self.facts.child.push((id, 0, key));
                    let value = self.expr(&key_value.value)?;
                    self.facts.child.push((id, 1, value));
                    Ok(id)
                }
                Prop::Method(method) => {
                    let id = self.node(NodeKind::ObjectMethod);
                    let key = self.prop_key(&method.key)?;
                    self.facts.child.push((id, 0, key));
                    let function = self.function(&method.function)?;
                    self.facts.child.push((id, 1, function));
                    Ok(id)
                }
                Prop::Getter(getter) => {
                    let id = self.node(NodeKind::Getter);
                    let key = self.prop_key(&getter.key)?;
                    self.facts.child.push((id, 0, key));
                    if let Some(body) = &getter.body {
                        let body = self.block(body)?;
                        self.facts.child.push((id, 1, body));
                    }
                    Ok(id)
                }
                Prop::Setter(setter) => {
                    let id = self.node(NodeKind::Setter);
                    let key = self.prop_key(&setter.key)?;
                    self.facts.child.push((id, 0, key));
                    let param = self.pat(&setter.param)?;
                    self.facts.child.push((id, 1, param));
                    if let Some(body) = &setter.body {
                        let body = self.block(body)?;
                        self.facts.child.push((id, 2, body));
                    }
                    Ok(id)
                }
                Prop::Assign(assign) => {
                    let id = self.node(NodeKind::AssignProp);
                    self.facts.ident_name.push((id, assign.key.sym.to_string()));
                    let value = self.expr(&assign.value)?;
                    self.facts.child.push((id, 0, value));
                    Ok(id)
                }
            },
        }
    }

    fn assign_target(&mut self, target: &AssignTarget) -> Result<NodeId, Unsupported> {
        match target {
            AssignTarget::Simple(SimpleAssignTarget::Ident(binding)) => {
                let id = self.node(NodeKind::Ident);
                self.facts.ident_name.push((id, binding.id.sym.to_string()));
                Ok(id)
            }
            AssignTarget::Simple(SimpleAssignTarget::Member(member)) => self.member(member),
            AssignTarget::Pat(AssignTargetPat::Array(array)) => self.array_pat(array),
            AssignTarget::Pat(AssignTargetPat::Object(object)) => self.object_pat(object),
            _ => unsupported("assign: target"),
        }
    }

    fn push_args(
        &mut self,
        parent: NodeId,
        base: u32,
        args: &[ExprOrSpread],
    ) -> Result<(), Unsupported> {
        for (index, arg) in args.iter().enumerate() {
            let child = self.expr_or_spread(arg)?;
            self.facts.child.push((parent, base + index as u32, child));
        }
        Ok(())
    }

    fn expr_or_spread(&mut self, arg: &ExprOrSpread) -> Result<NodeId, Unsupported> {
        let value = self.expr(&arg.expr)?;
        if arg.spread.is_some() {
            let spread = self.node(NodeKind::Spread);
            self.facts.child.push((spread, 0, value));
            Ok(spread)
        } else {
            Ok(value)
        }
    }

    fn member(&mut self, member: &MemberExpr) -> Result<NodeId, Unsupported> {
        let id = self.node(NodeKind::Member);
        let obj = self.expr(&member.obj)?;
        self.facts.child.push((id, 0, obj));
        match &member.prop {
            MemberProp::Ident(name) => {
                let prop = self.node(NodeKind::PropName);
                self.facts.prop_name.push((prop, name.sym.to_string()));
                self.facts.child.push((id, 1, prop));
            }
            MemberProp::Computed(computed) => {
                let computed = self.expr(&computed.expr)?;
                self.facts.child.push((id, 1, computed));
            }
            MemberProp::PrivateName(private_name) => {
                let prop = self.private_name_key(private_name);
                self.facts.child.push((id, 1, prop));
            }
        }
        Ok(id)
    }

    fn lit(&mut self, lit: &Lit) -> Result<NodeId, Unsupported> {
        match lit {
            Lit::Str(value) => {
                let id = self.node(NodeKind::StrLit);
                let text = js_ast::str_value(value);
                self.facts.str_lit.push((id, text));
                Ok(id)
            }
            Lit::Num(number) => {
                let id = self.node(NodeKind::NumLit);
                let token = number
                    .raw
                    .as_ref()
                    .map(|raw| raw.to_string())
                    .unwrap_or_else(|| number.value.to_string());
                self.facts.num_lit.push((id, token));
                Ok(id)
            }
            Lit::Bool(boolean) => {
                let id = self.node(NodeKind::BoolLit);
                self.facts.bool_lit.push((id, boolean.value));
                Ok(id)
            }
            Lit::Null(_) => Ok(self.node(NodeKind::NullLit)),
            Lit::BigInt(big_int) => {
                let id = self.node(NodeKind::BigIntLit);
                let token = big_int
                    .raw
                    .as_ref()
                    .map(|raw| raw.to_string())
                    .unwrap_or_else(|| big_int.value.to_string());
                self.facts.num_lit.push((id, token));
                Ok(id)
            }
            Lit::Regex(regex) => {
                let id = self.node(NodeKind::RegexLit);
                self.facts
                    .regex
                    .push((id, regex.exp.to_string(), regex.flags.to_string()));
                Ok(id)
            }
            _ => unsupported("lit"),
        }
    }
}

/// Project a parsed chunk's top-level statements into AST facts, or fail loudly
/// at the first construct not yet modeled.
pub fn extract_facts(module: &Module) -> Result<ChunkFacts, Unsupported> {
    extract_facts_items(&module.body)
}

/// Like [`extract_facts`], but over a borrowed item slice — so a caller projecting
/// one statement at a time (the resolver's per-needle facts) need not clone the
/// item into a one-item [`Module`] first.
pub fn extract_facts_items(items: &[ModuleItem]) -> Result<ChunkFacts, Unsupported> {
    let mut extractor = Extractor::default();
    for (body_idx, item) in items.iter().enumerate() {
        let ordinal = statement_ordinal_for_body_index(items, body_idx);
        extractor.module_item(item, ordinal)?;
    }
    Ok(extractor.facts)
}

/// One `obj.X` member-access derived from a statement's AST facts: the property
/// name `X`, plus the object identifier when the object is a bare `Ident`
/// (`ctx.X` ⟹ `object = Some("ctx")`; `foo().X` / `this.X` / `a.b.X` ⟹ `None`).
/// The owner-graph `reads_member` primitive's EDB row, projected from the same
/// `Member`→`PropName` structure [`Extractor::member`] records.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MemberReadFact {
    pub object: Option<String>,
    pub member: String,
}

/// The member accesses (`obj.X`, non-computed property) each top-level statement
/// performs, keyed by the statement's source-order ordinal — the EDB for the
/// `reads_member` selector primitive (the owner that reads member `.X`).
///
/// Derived from the per-statement AST facts ([`Member`]/[`PropName`]/[`Ident`]
/// projection). **Per-statement-tolerant** like [`coverage_report`] (and unlike
/// [`extract_facts`], which fails the whole chunk at the first unmodeled node): a
/// statement whose subtree hits an [`Unsupported`] construct contributes no
/// member-read rows rather than aborting the chunk. That tolerance is sound for a
/// *selector* primitive — a missing member-read can only make a `reads_member`
/// selector fail to resolve (fail-closed), never resolve to the wrong owner — and
/// it keeps the resolution path robust on real chunks the rest of the pipeline
/// already lowers. Computed member access (`obj[expr]`) carries no static
/// property name and contributes nothing.
pub fn member_reads_by_ordinal(module: &Module) -> BTreeMap<usize, Vec<MemberReadFact>> {
    let mut by_ordinal: BTreeMap<usize, Vec<MemberReadFact>> = BTreeMap::new();
    for (body_idx, item) in module.body.iter().enumerate() {
        let ordinal = statement_ordinal_for_body_index(&module.body, body_idx);
        let mut extractor = Extractor::default();
        // A statement with an unmodeled construct yields no member-read rows
        // (fail-closed for the selector), never a wrong row — so skip it.
        if extractor.module_item(item, ordinal).is_err() {
            continue;
        }
        let reads = member_reads_of_facts(&extractor.facts);
        if !reads.is_empty() {
            by_ordinal.entry(ordinal).or_default().extend(reads);
        }
    }
    by_ordinal
}

/// Index `facts.child` as parent -> (ordinal -> child) so an extractor can read a
/// node's positional children (callee + args, object + property, declarator
/// binding + init) by ordinal.
fn build_children_map(facts: &ChunkFacts) -> HashMap<NodeId, HashMap<u32, NodeId>> {
    let mut children: HashMap<NodeId, HashMap<u32, NodeId>> = HashMap::new();
    for (parent, ordinal, child) in &facts.child {
        children
            .entry(*parent)
            .or_default()
            .insert(*ordinal, *child);
    }
    children
}

/// Extract every `Member`-node `obj.X` from one statement's facts. A `Member`
/// node carries child ordinal 0 = object expression, child ordinal 1 = the
/// property: a `PropName` node (non-computed `.X`) or an arbitrary expression
/// (computed `[expr]`). Only non-computed accesses — a `PropName` child with a
/// static `prop_name` — are member reads; the object identifier is recorded when
/// child 0 is a bare `Ident`.
fn member_reads_of_facts(facts: &ChunkFacts) -> Vec<MemberReadFact> {
    let node_kind: HashMap<NodeId, NodeKind> = facts.node_kind.iter().copied().collect();
    let prop_name: HashMap<NodeId, &str> = facts
        .prop_name
        .iter()
        .map(|(id, name)| (*id, name.as_str()))
        .collect();
    let ident_name: HashMap<NodeId, &str> = facts
        .ident_name
        .iter()
        .map(|(id, name)| (*id, name.as_str()))
        .collect();
    let children = build_children_map(facts);
    facts
        .node_kind
        .iter()
        .filter(|(_, kind)| *kind == NodeKind::Member)
        .filter_map(|(member_node, _)| {
            let positional = children.get(member_node)?;
            let prop_node = positional.get(&1)?;
            // Non-computed access only: child 1 must be a `PropName` carrying `.X`.
            let member = prop_name.get(prop_node)?;
            let object = positional
                .get(&0)
                .filter(|obj| node_kind.get(obj) == Some(&NodeKind::Ident))
                .and_then(|obj| ident_name.get(obj))
                .map(|name| name.to_string());
            Some(MemberReadFact {
                object,
                member: member.to_string(),
            })
        })
        .collect()
}

/// One `mod.X` **use-site** derived from a statement's AST facts joined to the
/// chunk's import table: the import **source module** `mod` resolves to
/// (`"./codegen"`, `"react"`) and the export `member` consumed off it. The
/// owner-graph `member_of_module` primitive's EDB row — the first use-site edge,
/// pinning an entity by *how it is consumed* rather than by its own minified
/// name. Both labels are re-minify-invariant (module specifiers and export names
/// are the public API), which is what makes the edge survive a rebuild.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ModuleMemberUseFact {
    pub module: String,
    pub member: String,
}

/// The module-member uses (`mod.X`, `mod` a chunk-top imported binding) each
/// top-level statement performs, keyed by the statement's source-order ordinal —
/// the EDB for the `member_of_module` selector primitive (the entity consumed as
/// `mod.X`).
///
/// Built by projecting the same `Member`→`PropName` member accesses
/// [`member_reads_by_ordinal`] derives, then **joining the bare-identifier object
/// to `import_sources`** (local import binding name → import source specifier);
/// a member access whose object is not a chunk-top imported binding contributes
/// nothing. **Per-statement-tolerant** like [`member_reads_by_ordinal`], and
/// fail-closed-sound for the same reason. Computed access (`mod[expr]`) carries no
/// static member name and contributes nothing.
pub fn module_member_uses_by_ordinal(
    module: &Module,
    import_sources: &HashMap<String, String>,
) -> BTreeMap<usize, Vec<ModuleMemberUseFact>> {
    let mut by_ordinal: BTreeMap<usize, Vec<ModuleMemberUseFact>> = BTreeMap::new();
    for (ordinal, reads) in member_reads_by_ordinal(module) {
        let uses: Vec<ModuleMemberUseFact> = reads
            .into_iter()
            .filter_map(|read| {
                let object = read.object?;
                let source = import_sources.get(&object)?;
                Some(ModuleMemberUseFact {
                    module: source.clone(),
                    member: read.member,
                })
            })
            .collect();
        if !uses.is_empty() {
            by_ordinal.insert(ordinal, uses);
        }
    }
    by_ordinal
}

/// One call-site where a chunk-top binding is **passed as an argument**: the
/// argument binding (the target — a top-level identifier handed to the call), the
/// callee member name `.method` of the call (`r.register(Target)` ⟹
/// `callee_member = "register"`), the callee's object identifier when it is a bare
/// `Ident` (`r.register(...)` ⟹ `callee_object = Some("r")`; `a.b.register(...)`
/// / `register(...)` ⟹ `None`), and the 0-based argument position. The EDB row for
/// the `passed_to_call` selector primitive — the `resolves_to`-of-argument edge:
/// the target's identity is "the thing passed to `@registry.register`", which a
/// re-minification cannot rewrite (the callee member name and the registry's own
/// stable identity are the public API). Unlike [`MemberReadFact`] /
/// [`ModuleMemberUseFact`], this fact is keyed by the **argument binding name**
/// (the target's own minified binding), not by the owner that contains the call —
/// the target is a *separately-declared* owner, and the call site that names it
/// lives elsewhere (typically an anonymous registration statement).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CallArgumentFact {
    /// The minified binding passed as the argument — the target. Joined to its
    /// declaring owner via the owner graph's `name_owner`.
    pub argument: String,
    /// The callee member name `.method` of the call.
    pub callee_member: String,
    /// The callee's object identifier when it is a bare `Ident`; `None` for a
    /// deeper member chain or a bare callee.
    pub callee_object: Option<String>,
    /// The 0-based position of the argument in the call.
    pub arg_index: usize,
}

/// Every call-site argument-pass of a bare chunk-top identifier across the whole
/// chunk, as [`CallArgumentFact`] rows — the EDB for the `passed_to_call` selector
/// primitive (the target that is *passed to* `@object.member(...)`).
///
/// Derived from the chunk's AST facts: each `Call` / `New` / `OptCall` node whose
/// callee is a member access `obj.method` (non-computed) contributes one row per
/// argument that is a bare `Ident`, recording the argument's name, the callee
/// member, the callee object (when bare), and the argument index. The whole chunk
/// is scanned (not per top-level statement) because the registration call site
/// may be an anonymous statement, a nested call, or a body statement anywhere; the
/// rows are keyed by the *argument* identifier, so the containing statement does
/// not matter.
///
/// **Per-statement-tolerant** and fail-closed-sound for the same reason as
/// [`member_reads_by_ordinal`]: a statement whose subtree hits an [`Unsupported`]
/// construct contributes no rows rather than aborting the chunk — a missing row
/// can only make a `passed_to_call` selector fail to resolve (fail-closed), never
/// resolve to the wrong owner. A call with a computed callee (`r[expr](...)`), a
/// non-member callee (`register(...)`), or a non-identifier argument (`new X()`,
/// an object literal, a spread) contributes nothing — those carry no static
/// argument-binding to pin a target by.
pub fn call_argument_uses(module: &Module) -> Vec<CallArgumentFact> {
    let mut facts = Vec::new();
    for item in &module.body {
        let mut extractor = Extractor::default();
        // A statement with an unmodeled construct yields no call-argument rows
        // (fail-closed for the selector), never a wrong row — so skip it.
        if extractor.module_item(item, 0).is_err() {
            continue;
        }
        facts.extend(call_arguments_of_facts(&extractor.facts));
    }
    facts
}

/// Extract every call-argument-pass `obj.method(.., arg, ..)` of a bare `Ident`
/// argument from one statement's facts. A `Call` / `New` / `OptCall` node carries
/// child ordinal 0 = callee, child ordinals 1.. = arguments. The callee is a
/// member access when it is a `Member` node (child 0 = object, child 1 = property
/// `PropName`); only non-computed callees with a static member name contribute,
/// and only arguments that are bare `Ident` nodes (the static target binding).
fn call_arguments_of_facts(facts: &ChunkFacts) -> Vec<CallArgumentFact> {
    let node_kind: HashMap<NodeId, NodeKind> = facts.node_kind.iter().copied().collect();
    let prop_name: HashMap<NodeId, &str> = facts
        .prop_name
        .iter()
        .map(|(id, name)| (*id, name.as_str()))
        .collect();
    let ident_name: HashMap<NodeId, &str> = facts
        .ident_name
        .iter()
        .map(|(id, name)| (*id, name.as_str()))
        .collect();
    let children = build_children_map(facts);
    let mut rows = Vec::new();
    for (call_node, kind) in &facts.node_kind {
        if !matches!(*kind, NodeKind::Call | NodeKind::New | NodeKind::OptCall) {
            continue;
        }
        let Some(positional) = children.get(call_node) else {
            continue;
        };
        // The callee (child 0) must be a non-computed member access `obj.method`.
        let Some(callee) = positional.get(&0) else {
            continue;
        };
        if node_kind.get(callee) != Some(&NodeKind::Member) {
            continue;
        }
        let Some(callee_children) = children.get(callee) else {
            continue;
        };
        let Some(callee_member) = callee_children.get(&1).and_then(|p| prop_name.get(p)) else {
            continue;
        };
        let callee_object = callee_children
            .get(&0)
            .filter(|obj| node_kind.get(obj) == Some(&NodeKind::Ident))
            .and_then(|obj| ident_name.get(obj))
            .map(|name| name.to_string());
        // Arguments are children at ordinals 1.., contiguous from the call node.
        for arg_index in 0.. {
            let Some(arg_node) = positional.get(&(arg_index + 1)) else {
                break;
            };
            // Only a bare-identifier argument names a static target binding.
            if node_kind.get(arg_node) == Some(&NodeKind::Ident)
                && let Some(argument) = ident_name.get(arg_node)
            {
                rows.push(CallArgumentFact {
                    argument: argument.to_string(),
                    callee_member: callee_member.to_string(),
                    callee_object: callee_object.clone(),
                    arg_index: arg_index as usize,
                });
            }
        }
    }
    rows
}

/// One top-level **decorator-application** call `H([decorators], C.prototype,
/// "method"[, flags])` (or the class-decorator form `H([decorators], C)`), as the
/// EDB for the `makes_decorate_call` selector primitive. The **target is the
/// callee** `H` — the esbuild/TypeScript `__decorate` helper — distinguished by the
/// decorator application it makes, not by its own (byte-identical-across-modules)
/// body or its minified name. This is the inverse direction of `passed_to_call`:
/// there the target is *passed as an argument* to a call; here the target *makes*
/// the call, and the disambiguating anchor is the **decorated class** `C` (a
/// separately-pinned entity), which rides the re-minify-invariant `resolves_to`
/// edge — never the helper's own churn-prone name.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DecorateCallFact {
    /// The minified binding of the callee — the decorate helper, the target.
    /// Joined to its declaring owner via the owner graph's `name_owner`.
    pub callee: String,
    /// The minified binding of the decorated class: the base of the 2nd argument
    /// `C.prototype` (property/method-decorator form) or the bare 2nd argument `C`
    /// (class-decorator form). The anchor a `makes_decorate_call` selector rides
    /// (`class: @ClassAnchor`) — re-minify-invariant via the class's own selector.
    pub class_anchor: String,
    /// The decorated member name: the 3rd-argument string literal (`"isVisible"`).
    /// `None` for the 2-argument class-decorator form, which carries no member
    /// literal. Lets a selector narrow to a specific decorated member.
    pub member: Option<String>,
}

/// Every top-level **decorator-application** call across the chunk, as
/// [`DecorateCallFact`] rows — the EDB for the `makes_decorate_call` selector
/// primitive (the helper that *makes* a decorator application on `@ClassAnchor`).
///
/// Recognizes the esbuild/TypeScript `__decorate` shape structurally:
///
/// - **property / method decorator** — `H([d1, d2], C.prototype, "m", flags)`:
///   callee `H` a bare `Ident` (the target), 1st arg an array literal (the
///   decorator list), 2nd arg `C.prototype` (a non-computed `.prototype` member off
///   a bare-identifier class base), 3rd arg a string literal (the member name);
/// - **class decorator** — `H([d1], C)`: callee `H` a bare `Ident`, 1st arg an
///   array literal, 2nd arg a bare-identifier class base.
///
/// The 1st-argument array literal is the structural guard that makes this a
/// decorate application and not an arbitrary 2/3-argument call. Both anchor labels
/// the selector rides — the decorated class `C` (joined through `resolves_to` to a
/// pinned `@ClassAnchor`) and the optional member literal — survive a rebuild,
/// while the helper's own minified name and body do not.
///
/// **Per-statement-tolerant** and fail-closed-sound for the same reason as
/// [`call_argument_uses`]: a statement whose subtree hits an [`Unsupported`]
/// construct contributes no rows rather than aborting the chunk — a missing row can
/// only make a `makes_decorate_call` selector fail to resolve (fail-closed), never
/// resolve to the wrong owner. Only top-level statements are scanned: an esbuild
/// decorator application is always emitted as a bare top-level call adjacent to the
/// class it decorates.
pub fn decorate_call_uses(module: &Module) -> Vec<DecorateCallFact> {
    let mut facts = Vec::new();
    for item in &module.body {
        let mut extractor = Extractor::default();
        // A statement with an unmodeled construct yields no decorate-call rows
        // (fail-closed for the selector), never a wrong row — so skip it.
        if extractor.module_item(item, 0).is_err() {
            continue;
        }
        facts.extend(decorate_calls_of_facts(&extractor.facts));
    }
    facts
}

/// Extract every decorator-application `H([...], C.prototype, "m"[, flags])` /
/// `H([...], C)` from one statement's facts. A `Call` node carries child ordinal
/// 0 = callee, child ordinals 1.. = arguments. The callee must be a bare `Ident`
/// (the target helper); arg 0 (child 1) must be an `Array` node (the decorator
/// list); arg 1 (child 2) is the class anchor (`C.prototype` member or bare `C`);
/// arg 2 (child 3), when present, is the member-name `StrLit`.
fn decorate_calls_of_facts(facts: &ChunkFacts) -> Vec<DecorateCallFact> {
    let node_kind: HashMap<NodeId, NodeKind> = facts.node_kind.iter().copied().collect();
    let prop_name: HashMap<NodeId, &str> = facts
        .prop_name
        .iter()
        .map(|(id, name)| (*id, name.as_str()))
        .collect();
    let ident_name: HashMap<NodeId, &str> = facts
        .ident_name
        .iter()
        .map(|(id, name)| (*id, name.as_str()))
        .collect();
    let str_lit: HashMap<NodeId, &str> = facts
        .str_lit
        .iter()
        .map(|(id, value)| (*id, value.as_str()))
        .collect();
    let num_lit: HashMap<NodeId, &str> = facts
        .num_lit
        .iter()
        .map(|(id, value)| (*id, value.as_str()))
        .collect();
    let children = build_children_map(facts);
    let mut rows = Vec::new();
    for (call_node, kind) in &facts.node_kind {
        // A plain `Call`; `New`/`OptCall` are never an esbuild decorate application.
        if *kind != NodeKind::Call {
            continue;
        }
        let Some(positional) = children.get(call_node) else {
            continue;
        };
        // Callee (child 0) must be a bare identifier — the helper, our target.
        let Some(callee) = positional
            .get(&0)
            .filter(|c| node_kind.get(c) == Some(&NodeKind::Ident))
            .and_then(|c| ident_name.get(c))
        else {
            continue;
        };
        // Arg 0 (child 1) must be an array literal — the decorator list. This is
        // the structural guard distinguishing a decorate call from any 2/3-arg call.
        let Some(arg0) = positional.get(&1) else {
            continue;
        };
        if node_kind.get(arg0) != Some(&NodeKind::Array) {
            continue;
        }
        // Arg 1 (child 2) is the class anchor: `C.prototype` or bare `C`.
        let Some(arg1) = positional.get(&2) else {
            continue;
        };
        let Some(class_anchor) =
            decorate_class_anchor(arg1, &node_kind, &prop_name, &ident_name, &children)
        else {
            continue;
        };
        // A 5th-or-later positional argument means this is not the decorate shape.
        // (The property form is at most 4: array, class, member, flags.)
        if positional.contains_key(&5) {
            continue;
        }
        // Arg 2 (child 3) distinguishes the two forms: present ⟹ property/method
        // decorator carrying the member-name string literal (and an optional numeric
        // `flags` arg at child 4); absent ⟹ class-decorator form (no member, and no
        // arg may follow). esbuild emits flags as `1` / `2`.
        let member = match positional.get(&3) {
            // Class-decorator form: a member literal must not be present, and no
            // 4th argument may follow.
            None => {
                if positional.contains_key(&4) {
                    continue;
                }
                None
            }
            Some(arg2) => {
                let Some(value) = str_lit.get(arg2) else {
                    continue;
                };
                // The flags argument (child 4), when present, must be numeric.
                if let Some(flags) = positional.get(&4)
                    && !num_lit.contains_key(flags)
                {
                    continue;
                }
                Some(value.to_string())
            }
        };
        rows.push(DecorateCallFact {
            callee: callee.to_string(),
            class_anchor,
            member,
        });
    }
    rows
}

/// The decorated-class binding of a decorate application's 2nd argument: the
/// bare-identifier base of a non-computed `C.prototype` member (property/method
/// form) or the bare identifier `C` itself (class-decorator form). Any other shape
/// (computed access, deeper chain, `.prototype` off a non-identifier, a member
/// other than `.prototype`) yields `None`, so the statement contributes no
/// decorate-call row (fail-closed).
fn decorate_class_anchor(
    arg: &NodeId,
    node_kind: &HashMap<NodeId, NodeKind>,
    prop_name: &HashMap<NodeId, &str>,
    ident_name: &HashMap<NodeId, &str>,
    children: &HashMap<NodeId, HashMap<u32, NodeId>>,
) -> Option<String> {
    match node_kind.get(arg) {
        // Class-decorator form: bare `C`.
        Some(&NodeKind::Ident) => ident_name.get(arg).map(|name| name.to_string()),
        // Property/method form: `C.prototype` — child 0 a bare ident `C`, child 1
        // the `PropName` `prototype`.
        Some(&NodeKind::Member) => {
            let member_children = children.get(arg)?;
            let prop = member_children.get(&1)?;
            if prop_name.get(prop).copied() != Some("prototype") {
                return None;
            }
            member_children
                .get(&0)
                .filter(|base| node_kind.get(base) == Some(&NodeKind::Ident))
                .and_then(|base| ident_name.get(base))
                .map(|name| name.to_string())
        }
        _ => None,
    }
}

/// One top-level `var X = Object.<property>` declarator that aliases an **intrinsic
/// method off the unshadowed global `Object`**, as the EDB for the `intrinsic_alias`
/// selector primitive. This is the structural recognition half of the primitive that
/// retires the esbuild decorate-trio's two companions — `var X =
/// Object.defineProperty` / `var X = Object.getOwnPropertyDescriptor`. The companions
/// have no anchor in their own body (N byte-identical copies across modules, and the
/// anchor is the global `Object`, not a spec member), so no `source_match` can pin
/// them; the resolver pairs this structural fact with an inverse-`references` edge
/// ("the alias *referenced by* `@<decorateHelper>`") to pick the unique copy.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IntrinsicAliasFact {
    /// The minified binding of the alias — the target. Joined to its declaring
    /// owner via the owner graph's `name_owner`.
    pub binding: String,
    /// The intrinsic property name read off `Object` (`defineProperty`,
    /// `getOwnPropertyDescriptor`). The re-minify-invariant label half of the
    /// selector — a spec-level method name the bundler does not rewrite.
    pub property: String,
}

/// Every top-level `var X = Object.<property>` declarator aliasing an intrinsic
/// method off the **unshadowed global `Object`**, as [`IntrinsicAliasFact`] rows —
/// the EDB for the `intrinsic_alias` selector primitive.
///
/// The genuine-intrinsic-identity guard is **fail-closed**: a chunk whose
/// top-level declarations or imports bind the name `Object` (a shadowed or
/// reassigned `Object`) is treated as having *no* intrinsic `Object`, so it yields
/// **no rows** — the alias might read off a local `Object`, not the global, and a
/// wrong pin is worse than no pin. This mirrors
/// [`analysis::facts`]'s `unshadowed_global_object_aliases` for the
/// `globalThis`/`window` family; `Object` itself is added here because it is the
/// intrinsic whose `.defineProperty` / `.getOwnPropertyDescriptor` the esbuild
/// decorate helper reads.
///
/// Recognizes the alias structurally: a `VarDeclarator` whose name is a bare
/// `BindingIdent` and whose initializer is a non-computed member access
/// `Object.<property>` off a bare-identifier base spelled exactly `Object`. The
/// declaration keyword (`var`/`let`/`const`) is not constrained — esbuild emits
/// `var`, but the structural shape is the invariant.
///
/// **Per-statement-tolerant** and fail-closed-sound for the same reason as
/// [`decorate_call_uses`]: a statement whose subtree hits an [`Unsupported`]
/// construct contributes no rows rather than aborting the chunk — a missing row can
/// only make an `intrinsic_alias` selector fail to resolve (fail-closed), never
/// resolve to the wrong owner. Only top-level statements are scanned: the esbuild
/// companion aliases are always bare top-level declarations.
pub fn intrinsic_alias_uses(module: &Module) -> Vec<IntrinsicAliasFact> {
    // Fail-closed: a shadowed/reassigned/imported `Object` defeats the
    // intrinsic-identity guard, so the whole chunk yields no rows.
    if module_top_level_binds_object(module) {
        return Vec::new();
    }
    let mut facts = Vec::new();
    for item in &module.body {
        let mut extractor = Extractor::default();
        // A statement with an unmodeled construct yields no intrinsic-alias rows
        // (fail-closed for the selector), never a wrong row — so skip it.
        if extractor.module_item(item, 0).is_err() {
            continue;
        }
        facts.extend(intrinsic_aliases_of_facts(&extractor.facts));
    }
    facts
}

/// `true` when any top-level declaration (incl. block-hoisted `var`s) or import
/// specifier of `module` binds the name `Object`, shadowing the global intrinsic.
/// The structural sibling of `analysis::facts::unshadowed_global_object_aliases`'s
/// shadow scan, specialized to the single name `Object`; kept here (not borrowed
/// from the `analysis` crate) so `chunk_facts` stays dependency-light.
fn module_top_level_binds_object(module: &Module) -> bool {
    module.body.iter().any(|item| match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => decl_binds_object(decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => decl_binds_object(&export.decl),
        ModuleItem::ModuleDecl(ModuleDecl::Import(import)) => {
            import.specifiers.iter().any(|spec| {
                let local = match spec {
                    ImportSpecifier::Named(named) => &named.local.sym,
                    ImportSpecifier::Default(default) => &default.local.sym,
                    ImportSpecifier::Namespace(namespace) => &namespace.local.sym,
                };
                local.as_ref() == "Object"
            })
        }
        // A bare `Object = ...` assignment (reassignment of the global) shadows the
        // intrinsic identity just as a declaration does; treat it as binding.
        ModuleItem::Stmt(stmt) => stmt_reassigns_object(stmt),
        _ => false,
    })
}

/// `true` when a declaration binds the name `Object` at the top level. A `var`
/// hoists out of blocks, so its name may appear nested; a `let`/`const`/`function`/
/// `class` binds directly.
fn decl_binds_object(decl: &Decl) -> bool {
    match decl {
        Decl::Class(class) => class.ident.sym.as_ref() == "Object",
        Decl::Fn(func) => func.ident.sym.as_ref() == "Object",
        Decl::Var(var) => var
            .decls
            .iter()
            .any(|declarator| pat_binds_object(&declarator.name)),
        _ => false,
    }
}

/// `true` when a top-level statement reassigns the bare global `Object`
/// (`Object = ...`). A reassignment defeats the intrinsic-identity guard exactly
/// like a shadowing declaration, so the alias must fail closed.
fn stmt_reassigns_object(stmt: &Stmt) -> bool {
    let Stmt::Expr(expr_stmt) = stmt else {
        return false;
    };
    let Expr::Assign(assign) = expr_stmt.expr.as_ref() else {
        return false;
    };
    matches!(
        &assign.left,
        AssignTarget::Simple(SimpleAssignTarget::Ident(ident)) if ident.id.sym.as_ref() == "Object"
    )
}

/// `true` when a binding pattern binds the name `Object` anywhere within it
/// (`Object`, `{ Object }`, `[Object]`, …). A `var Object` shadows the global.
fn pat_binds_object(pat: &Pat) -> bool {
    match pat {
        Pat::Ident(binding) => binding.id.sym.as_ref() == "Object",
        Pat::Array(array) => array.elems.iter().flatten().any(pat_binds_object),
        Pat::Object(object) => object.props.iter().any(|prop| match prop {
            ObjectPatProp::KeyValue(kv) => pat_binds_object(&kv.value),
            ObjectPatProp::Assign(assign) => assign.key.id.sym.as_ref() == "Object",
            ObjectPatProp::Rest(rest) => pat_binds_object(&rest.arg),
        }),
        Pat::Rest(rest) => pat_binds_object(&rest.arg),
        Pat::Assign(assign) => pat_binds_object(&assign.left),
        _ => false,
    }
}

/// Extract every `var X = Object.<property>` intrinsic-alias declarator from one
/// statement's facts. A `VarDeclarator` node carries child 0 = name pattern, child
/// 1 = initializer; only a bare-`BindingIdent` name and a non-computed
/// `Object.<property>` member initializer (base a bare `Ident` spelled `Object`)
/// contribute. The unshadowed-`Object` guard is applied by the caller, so a base
/// spelled `Object` here is the genuine global intrinsic.
fn intrinsic_aliases_of_facts(facts: &ChunkFacts) -> Vec<IntrinsicAliasFact> {
    let node_kind: HashMap<NodeId, NodeKind> = facts.node_kind.iter().copied().collect();
    let prop_name: HashMap<NodeId, &str> = facts
        .prop_name
        .iter()
        .map(|(id, name)| (*id, name.as_str()))
        .collect();
    let ident_name: HashMap<NodeId, &str> = facts
        .ident_name
        .iter()
        .map(|(id, name)| (*id, name.as_str()))
        .collect();
    let children = build_children_map(facts);
    let mut rows = Vec::new();
    for (declarator_node, kind) in &facts.node_kind {
        if *kind != NodeKind::VarDeclarator {
            continue;
        }
        let Some(declarator_children) = children.get(declarator_node) else {
            continue;
        };
        // The name (child 0) must be a bare binding identifier — the alias target.
        let Some(binding) = declarator_children
            .get(&0)
            .filter(|name| node_kind.get(name) == Some(&NodeKind::BindingIdent))
            .and_then(|name| ident_name.get(name))
        else {
            continue;
        };
        // The initializer (child 1) must be a non-computed `Object.<property>`
        // member access off a bare-identifier base spelled exactly `Object`.
        let Some(init) = declarator_children
            .get(&1)
            .filter(|init| node_kind.get(init) == Some(&NodeKind::Member))
        else {
            continue;
        };
        let Some(member_children) = children.get(init) else {
            continue;
        };
        let base_is_object = member_children
            .get(&0)
            .filter(|base| node_kind.get(base) == Some(&NodeKind::Ident))
            .and_then(|base| ident_name.get(base))
            == Some(&"Object");
        if !base_is_object {
            continue;
        }
        let Some(property) = member_children.get(&1).and_then(|prop| prop_name.get(prop)) else {
            continue;
        };
        rows.push(IntrinsicAliasFact {
            binding: binding.to_string(),
            property: property.to_string(),
        });
    }
    rows
}

/// Per-top-level-statement coverage of the extractor over a chunk: how many
/// statements fully extract vs. hit a fail-closed [`Unsupported`]. This is the
/// instrument that turns P1 growth into a prioritized worklist — run it over a
/// real chunk and grow the walk to clear the most frequent blocker first.
#[derive(Debug, Default)]
pub struct CoverageReport {
    pub total: usize,
    pub covered: usize,
    /// `Unsupported.context` -> count. Each top-level statement contributes its
    /// **first** blocker (the walk bails at the first unmodeled node), so this
    /// ranks "what to model next", not every gap in the subtree.
    pub unsupported: BTreeMap<&'static str, usize>,
}

/// Attempt extraction of each top-level statement independently and tally the
/// outcome. Unlike [`extract_facts`], one statement's `Unsupported` does not
/// abort the others — the point is to measure how far coverage reaches.
pub fn coverage_report(module: &Module) -> CoverageReport {
    let mut report = CoverageReport::default();
    for (body_idx, item) in module.body.iter().enumerate() {
        report.total += 1;
        let ordinal = statement_ordinal_for_body_index(&module.body, body_idx);
        let mut extractor = Extractor::default();
        match extractor.module_item(item, ordinal) {
            Ok(_) => report.covered += 1,
            Err(Unsupported { context }) => *report.unsupported.entry(context).or_default() += 1,
        }
    }
    report
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::{BTreeSet, HashSet};

    fn extract(src: &str) -> Result<ChunkFacts, Unsupported> {
        js_ast::with_swc_globals(|| {
            extract_facts(&js_ast::parse_js_module_ast("<test>", src).unwrap())
        })
    }

    #[test]
    fn extracts_member_call_with_string_arg_faithfully() {
        let facts = extract("const x = foo.bar(\"hello\");").expect("covered shape extracts");

        let idents: BTreeSet<&str> = facts.ident_name.iter().map(|(_, s)| s.as_str()).collect();
        assert!(idents.contains("x"), "declarator name present: {idents:?}");
        assert!(idents.contains("foo"), "member object present: {idents:?}");

        let props: Vec<&str> = facts.prop_name.iter().map(|(_, s)| s.as_str()).collect();
        assert_eq!(props, vec!["bar"], "member property name");
        let strings: Vec<&str> = facts.str_lit.iter().map(|(_, s)| s.as_str()).collect();
        assert_eq!(strings, vec!["hello"], "string-literal argument value");

        let kinds: HashSet<NodeKind> = facts.node_kind.iter().map(|(_, k)| *k).collect();
        for expected in [
            NodeKind::VarDecl,
            NodeKind::VarDeclarator,
            NodeKind::BindingIdent,
            NodeKind::Call,
            NodeKind::Member,
            NodeKind::PropName,
            NodeKind::Ident,
            NodeKind::StrLit,
        ] {
            assert!(
                kinds.contains(&expected),
                "kind {expected:?} present: {kinds:?}"
            );
        }

        // Every child edge references a node that exists (the projection is a tree).
        let nodes: BTreeSet<NodeId> = facts.node_kind.iter().map(|(id, _)| *id).collect();
        for (parent, _, child) in &facts.child {
            assert!(
                nodes.contains(parent) && nodes.contains(child),
                "child edge ({parent},{child}) references an unknown node",
            );
        }

        // One top-level statement, owner ordinal 0.
        assert_eq!(facts.top_level.len(), 1);
        assert_eq!(facts.top_level[0].1, 0);
    }

    #[test]
    fn extracts_function_declaration_body() {
        // The bare-delegator shape — the motivating `isMeetingTranscriptionProvider`
        // example: a function whose only identity is the call in its body.
        let facts = extract("function f(x) { return g(x); }").expect("covered shape extracts");

        let idents: BTreeSet<&str> = facts.ident_name.iter().map(|(_, s)| s.as_str()).collect();
        for name in ["f", "x", "g"] {
            assert!(idents.contains(name), "ident {name} present: {idents:?}");
        }
        let kinds: HashSet<NodeKind> = facts.node_kind.iter().map(|(_, k)| *k).collect();
        for expected in [
            NodeKind::FnDecl,
            NodeKind::Function,
            NodeKind::Block,
            NodeKind::Return,
            NodeKind::Call,
            NodeKind::Ident,
            NodeKind::BindingIdent,
        ] {
            assert!(
                kinds.contains(&expected),
                "kind {expected:?} present: {kinds:?}"
            );
        }
        assert_eq!(facts.top_level.len(), 1);
    }

    #[test]
    fn extracts_class_with_method_returning_literal() {
        // The DocumentAccessorFactory example: identity = a getName() returning
        // the class's own readable name, plus an `extends` edge.
        let facts = extract(
            "class DocumentAccessorFactory extends Base { getName() { return \"DocumentAccessorFactory\"; } }",
        )
        .expect("covered shape extracts");

        let idents: BTreeSet<&str> = facts.ident_name.iter().map(|(_, s)| s.as_str()).collect();
        assert!(
            idents.contains("DocumentAccessorFactory"),
            "class name: {idents:?}"
        );
        assert!(idents.contains("Base"), "super class: {idents:?}");
        let props: Vec<&str> = facts.prop_name.iter().map(|(_, s)| s.as_str()).collect();
        assert_eq!(props, vec!["getName"], "method name");
        let strings: Vec<&str> = facts.str_lit.iter().map(|(_, s)| s.as_str()).collect();
        assert_eq!(strings, vec!["DocumentAccessorFactory"], "returned literal");
        assert_eq!(facts.super_class.len(), 1, "one extends edge");

        let kinds: HashSet<NodeKind> = facts.node_kind.iter().map(|(_, k)| *k).collect();
        for expected in [
            NodeKind::ClassDecl,
            NodeKind::Class,
            NodeKind::Method,
            NodeKind::PropName,
            NodeKind::Function,
            NodeKind::Block,
            NodeKind::Return,
            NodeKind::StrLit,
        ] {
            assert!(
                kinds.contains(&expected),
                "kind {expected:?} present: {kinds:?}"
            );
        }
    }

    #[test]
    fn extracts_common_expression_variants() {
        // (b + c) ? new D(e) : [f] — binary, conditional, new, array.
        let facts = extract("const a = b + c ? new D(e) : [f];").expect("covered shape extracts");

        let kinds: HashSet<NodeKind> = facts.node_kind.iter().map(|(_, k)| *k).collect();
        for expected in [
            NodeKind::Cond,
            NodeKind::Bin,
            NodeKind::New,
            NodeKind::Array,
        ] {
            assert!(
                kinds.contains(&expected),
                "kind {expected:?} present: {kinds:?}"
            );
        }
        // The `const` declaration keyword is recorded as an operator-class label
        // (so a `let` selector cannot match a `const`), then the binary `+`.
        let operators: Vec<&str> = facts.operator.iter().map(|(_, s)| s.as_str()).collect();
        assert_eq!(
            operators,
            vec!["const", "+"],
            "decl keyword then binary operator"
        );
        let idents: BTreeSet<&str> = facts.ident_name.iter().map(|(_, s)| s.as_str()).collect();
        for name in ["a", "b", "c", "D", "e", "f"] {
            assert!(idents.contains(name), "ident {name} present: {idents:?}");
        }
    }

    #[test]
    fn extracts_function_expression_and_object_literal() {
        let facts = extract("const a = { handler: function (x) { return x; }, ...rest };")
            .expect("covered shape extracts");
        let kinds: HashSet<NodeKind> = facts.node_kind.iter().map(|(_, k)| *k).collect();
        for expected in [
            NodeKind::Object,
            NodeKind::KeyValue,
            NodeKind::FnExpr,
            NodeKind::Function,
            NodeKind::Spread,
        ] {
            assert!(
                kinds.contains(&expected),
                "kind {expected:?} present: {kinds:?}"
            );
        }
        let props: Vec<&str> = facts.prop_name.iter().map(|(_, s)| s.as_str()).collect();
        assert_eq!(props, vec!["handler"], "object key");
    }

    #[test]
    fn extracts_member_assignment() {
        // `a.b = c;` — the module-export assignment shape that dominates the
        // corpus after fn/object.
        let facts = extract("a.b = c;").expect("covered shape extracts");
        let kinds: HashSet<NodeKind> = facts.node_kind.iter().map(|(_, k)| *k).collect();
        for expected in [NodeKind::ExprStmt, NodeKind::Assign, NodeKind::Member] {
            assert!(
                kinds.contains(&expected),
                "kind {expected:?} present: {kinds:?}"
            );
        }
        assert!(
            facts.operator.iter().any(|(_, op)| op == "="),
            "assign operator: {:?}",
            facts.operator,
        );
        let props: Vec<&str> = facts.prop_name.iter().map(|(_, s)| s.as_str()).collect();
        assert_eq!(props, vec!["b"], "assigned member name");
    }

    #[test]
    fn extracts_control_flow_statements() {
        // if/else with a block consequent and a throw alternative.
        let facts = extract("if (a) { b(); } else throw c;").expect("covered shape extracts");
        let kinds: HashSet<NodeKind> = facts.node_kind.iter().map(|(_, k)| *k).collect();
        for expected in [
            NodeKind::If,
            NodeKind::Block,
            NodeKind::ExprStmt,
            NodeKind::Call,
            NodeKind::Throw,
        ] {
            assert!(
                kinds.contains(&expected),
                "kind {expected:?} present: {kinds:?}"
            );
        }
    }

    #[test]
    fn extracts_class_constructor_and_property() {
        let facts = extract("class C { x = 1; constructor(a) { this.a = a; } }")
            .expect("covered shape extracts");
        let kinds: HashSet<NodeKind> = facts.node_kind.iter().map(|(_, k)| *k).collect();
        for expected in [
            NodeKind::ClassDecl,
            NodeKind::ClassProp,
            NodeKind::Constructor,
            NodeKind::This,
            NodeKind::Assign,
        ] {
            assert!(
                kinds.contains(&expected),
                "kind {expected:?} present: {kinds:?}"
            );
        }
    }

    #[test]
    fn extracts_private_class_members_and_access() {
        let facts =
            extract("class C { #x = 1; #m() { return this.#x; } get(other) { return other.#x; } }")
                .expect("private members extract");
        let kinds: HashSet<NodeKind> = facts.node_kind.iter().map(|(_, k)| *k).collect();
        for expected in [NodeKind::ClassDecl, NodeKind::ClassProp, NodeKind::Method] {
            assert!(
                kinds.contains(&expected),
                "kind {expected:?} present: {kinds:?}"
            );
        }
        // The declaring `#x` key, the `this.#x` use, and the `other.#x` use
        // are all recorded under the same `#`-prefixed prop_name label.
        let x_count = facts
            .prop_name
            .iter()
            .filter(|(_, name)| name == "#x")
            .count();
        assert_eq!(x_count, 3, "prop_name entries: {:?}", facts.prop_name);
        assert!(
            facts.prop_name.iter().any(|(_, name)| name == "#m"),
            "prop_name entries: {:?}",
            facts.prop_name
        );
    }

    #[test]
    fn extracts_using_declarations() {
        let facts =
            extract("{ using a = open(); await using b = openAsync(); }").expect("using extracts");
        let operators: HashSet<&str> = facts.operator.iter().map(|(_, op)| op.as_str()).collect();
        assert!(operators.contains("using"), "operators: {operators:?}");
        assert!(
            operators.contains("await using"),
            "operators: {operators:?}"
        );
    }

    #[test]
    fn extracts_for_of_destructuring_and_template() {
        let facts = extract("for (const { a, b } of items) { log(`x${a}`); }")
            .expect("covered shape extracts");
        let kinds: HashSet<NodeKind> = facts.node_kind.iter().map(|(_, k)| *k).collect();
        for expected in [
            NodeKind::ForOf,
            NodeKind::ObjectPat,
            NodeKind::PatAssign,
            NodeKind::Tpl,
            NodeKind::TplQuasi,
        ] {
            assert!(
                kinds.contains(&expected),
                "kind {expected:?} present: {kinds:?}"
            );
        }
    }

    #[test]
    fn extracts_super_await_optional_chain() {
        let facts = extract("class C extends B { async m() { return await super.n()?.p; } }")
            .expect("covered shape extracts");
        let kinds: HashSet<NodeKind> = facts.node_kind.iter().map(|(_, k)| *k).collect();
        for expected in [NodeKind::SuperProp, NodeKind::Await, NodeKind::OptChain] {
            assert!(
                kinds.contains(&expected),
                "kind {expected:?} present: {kinds:?}"
            );
        }
    }

    #[test]
    fn extracts_module_imports_and_default_export() {
        let facts = extract("import { a, b } from \"m\"; export default function () {}")
            .expect("covered shape extracts");
        let kinds: HashSet<NodeKind> = facts.node_kind.iter().map(|(_, k)| *k).collect();
        for expected in [
            NodeKind::Import,
            NodeKind::ImportSpecifier,
            NodeKind::ExportDefaultDecl,
            NodeKind::Function,
        ] {
            assert!(
                kinds.contains(&expected),
                "kind {expected:?} present: {kinds:?}"
            );
        }
        let imported_module = facts.str_lit.iter().any(|(_, s)| s == "m");
        assert!(
            imported_module,
            "import source recorded: {:?}",
            facts.str_lit
        );
    }

    #[test]
    fn coverage_report_tallies_per_statement() {
        // One extractable statement; one blocked by an unmodeled class member.
        // `PrivateMethod`/`PrivateProp` (`#x`) are modeled now (see
        // `class_member`/`private_name_key`) — `TsIndexSignature` is the
        // still-unmodeled member kind used here as the canary. One
        // statement's gap does not abort the tally of the other.
        let report = js_ast::with_swc_globals(|| {
            coverage_report(
                &js_ast::parse_js_module_ast(
                    "<test>",
                    "const a = \"s\";\nclass C { [k: string]: number; }\n",
                )
                .unwrap(),
            )
        });
        assert_eq!(report.total, 2);
        assert_eq!(report.covered, 1);
        assert_eq!(report.unsupported.get("class_member"), Some(&1));
    }

    #[test]
    fn unmodeled_construct_errors_loudly_not_silently() {
        // A TS index signature class member stays unmodeled (TS-only syntax,
        // not something emitted JS ever needs). Fail-closed means a hard
        // error here, never a silently-incomplete fact set that would let a
        // query under-constrain.
        let error = extract("class C { [k: string]: number; }").unwrap_err();
        assert_eq!(error.context, "class_member");
    }

    #[test]
    fn extracts_meta_properties() {
        // `import.meta` / `new.target` are fixed meta-properties: a subject
        // statement using one must still project to facts (otherwise a needle
        // whose `STMT_LIST` would absorb it could never match the owner).
        let kinds: HashSet<NodeKind> = extract("const a = import.meta;")
            .expect("import.meta extracts")
            .node_kind
            .iter()
            .map(|(_, k)| *k)
            .collect();
        assert!(
            kinds.contains(&NodeKind::MetaPropImportMeta),
            "kinds: {kinds:?}"
        );
    }

    #[test]
    fn extracts_debugger_and_tagged_template() {
        // `debugger;` is a childless statement node; a tagged template carries
        // its tag plus the template (quasis interleaved with exprs).
        let facts = extract("function f() { debugger; }\nconst a = tag`x${y}z`;")
            .expect("debugger + tagged template extract");
        let kinds: HashSet<NodeKind> = facts.node_kind.iter().map(|(_, k)| *k).collect();
        for expected in [
            NodeKind::Debugger,
            NodeKind::TaggedTpl,
            NodeKind::Tpl,
            NodeKind::TplQuasi,
        ] {
            assert!(
                kinds.contains(&expected),
                "kind {expected:?} present: {kinds:?}"
            );
        }
    }

    fn member_reads(src: &str) -> BTreeMap<usize, Vec<MemberReadFact>> {
        js_ast::with_swc_globals(|| {
            member_reads_by_ordinal(&js_ast::parse_js_module_ast("<test>", src).unwrap())
        })
    }

    #[test]
    fn member_reads_by_ordinal_records_object_and_member() {
        // A helper reading `ctx.uniqueId` (object is a bare ident), and a second
        // statement reading `foo().label` (object is a call — no object ident).
        let reads =
            member_reads("function helper(ctx) { return ctx.uniqueId; }\nconst v = foo().label;\n");
        assert_eq!(
            reads[&0],
            vec![MemberReadFact {
                object: Some("ctx".to_string()),
                member: "uniqueId".to_string(),
            }],
        );
        assert_eq!(
            reads[&1],
            vec![MemberReadFact {
                object: None,
                member: "label".to_string(),
            }],
        );
    }

    #[test]
    fn member_reads_by_ordinal_skips_computed_access() {
        // `obj[expr]` is computed — no static property name — so it contributes no
        // member-read row, while the sibling `obj.kept` does.
        let reads = member_reads("function f(obj, k) { return obj[k] + obj.kept; }\n");
        assert_eq!(
            reads[&0],
            vec![MemberReadFact {
                object: Some("obj".to_string()),
                member: "kept".to_string(),
            }],
        );
    }

    fn module_member_uses(
        src: &str,
        import_sources: &[(&str, &str)],
    ) -> BTreeMap<usize, Vec<ModuleMemberUseFact>> {
        let imports: HashMap<String, String> = import_sources
            .iter()
            .map(|(local, src)| ((*local).to_string(), (*src).to_string()))
            .collect();
        js_ast::with_swc_globals(|| {
            module_member_uses_by_ordinal(
                &js_ast::parse_js_module_ast("<test>", src).unwrap(),
                &imports,
            )
        })
    }

    #[test]
    fn module_member_uses_joins_imported_object_to_source_module() {
        // `codegen` is imported from `./codegen`; the helper consuming
        // `codegen.emit` yields a use-site row keyed by the source module + export
        // name (both re-minify-invariant). A second helper reads `.emit` off a
        // *non-imported* local `local`, so it contributes nothing — the join to the
        // import table is what makes this a module-member use, not any member read.
        let uses = module_member_uses(
            "function a() { return codegen.emit(); }\nfunction b(local) { return local.emit(); }\n",
            &[("codegen", "./codegen")],
        );
        assert_eq!(
            uses[&0],
            vec![ModuleMemberUseFact {
                module: "./codegen".to_string(),
                member: "emit".to_string(),
            }],
        );
        assert!(
            !uses.contains_key(&1),
            "non-imported object contributes no row"
        );
    }

    #[test]
    fn module_member_uses_skips_computed_access() {
        // `mod[expr]` is computed — no static export name — so it contributes
        // nothing even though `mod` is imported; the sibling `mod.kept` does.
        let uses = module_member_uses(
            "function f(k) { return mod[k] + mod.kept; }\n",
            &[("mod", "./m")],
        );
        assert_eq!(
            uses[&0],
            vec![ModuleMemberUseFact {
                module: "./m".to_string(),
                member: "kept".to_string(),
            }],
        );
    }

    fn call_args(src: &str) -> Vec<CallArgumentFact> {
        js_ast::with_swc_globals(|| {
            call_argument_uses(&js_ast::parse_js_module_ast("<test>", src).unwrap())
        })
    }

    #[test]
    fn call_argument_uses_records_argument_callee_and_index() {
        // The registry shape: a top-level class passed to `r.register(FooAccessor)`
        // in a separate (anonymous) statement. The row is keyed by the *argument*
        // binding `FooAccessor` (the target), and records the callee member, the
        // callee object, and the argument index.
        let facts = call_args("class FooAccessor {}\nr.register(FooAccessor);\n");
        assert_eq!(
            facts,
            vec![CallArgumentFact {
                argument: "FooAccessor".to_string(),
                callee_member: "register".to_string(),
                callee_object: Some("r".to_string()),
                arg_index: 0,
            }],
        );
    }

    #[test]
    fn call_argument_uses_records_object_none_for_deep_callee() {
        // A deeper callee chain `a.b.register(X)` has no bare-ident object, so the
        // object is `None` but the callee member and argument are still recorded.
        let facts = call_args("a.b.register(X);\n");
        assert_eq!(
            facts,
            vec![CallArgumentFact {
                argument: "X".to_string(),
                callee_member: "register".to_string(),
                callee_object: None,
                arg_index: 0,
            }],
        );
    }

    #[test]
    fn call_argument_uses_records_index_for_later_positions() {
        // A registration call with metadata args before the target: the target
        // `Widget` is at argument index 1, which the fact records so a selector can
        // pin by position.
        let facts = call_args("h.define(\"widget\", Widget);\n");
        assert_eq!(
            facts,
            vec![CallArgumentFact {
                argument: "Widget".to_string(),
                callee_member: "define".to_string(),
                callee_object: Some("h".to_string()),
                arg_index: 1,
            }],
        );
    }

    #[test]
    fn call_argument_uses_skips_non_member_callee_and_non_ident_args() {
        // A bare callee (`register(X)`) has no callee member name to pin by, and a
        // non-identifier argument (`new Y()`, an object literal) names no static
        // target binding — both contribute nothing. Only the member-callee call
        // with a bare-ident argument yields a row.
        let facts = call_args(
            "register(X);\nr.register(new Y());\nr.register({ a: 1 });\nr.register(Kept);\n",
        );
        assert_eq!(
            facts,
            vec![CallArgumentFact {
                argument: "Kept".to_string(),
                callee_member: "register".to_string(),
                callee_object: Some("r".to_string()),
                arg_index: 0,
            }],
        );
    }

    #[test]
    fn call_argument_uses_tolerates_unmodeled_statement() {
        // A statement with an unmodeled construct (private class member) yields no
        // rows but does not abort the scan — the sibling registration still
        // contributes (fail-closed: a missing row can only fail to resolve, never
        // mis-resolve).
        let facts = call_args("class C { #x = 1; }\nr.register(Kept);\n");
        assert_eq!(
            facts,
            vec![CallArgumentFact {
                argument: "Kept".to_string(),
                callee_member: "register".to_string(),
                callee_object: Some("r".to_string()),
                arg_index: 0,
            }],
        );
    }

    fn decorate_calls(src: &str) -> Vec<DecorateCallFact> {
        js_ast::with_swc_globals(|| {
            decorate_call_uses(&js_ast::parse_js_module_ast("<test>", src).unwrap())
        })
    }

    #[test]
    fn decorate_call_uses_records_property_decorator_shape() {
        // The esbuild property-decorator shape: `H([d], C.prototype, "m", flags)`.
        // The row's target is the callee `H` (the helper), the anchor is the
        // decorated class `C`, and the member literal is recorded for narrowing.
        let facts = decorate_calls("H([d], C.prototype, \"isVisible\", 2);\n");
        assert_eq!(
            facts,
            vec![DecorateCallFact {
                callee: "H".to_string(),
                class_anchor: "C".to_string(),
                member: Some("isVisible".to_string()),
            }],
        );
    }

    #[test]
    fn decorate_call_uses_records_property_decorator_without_flags() {
        // The 3-argument property form `H([d], C.prototype, "m")` (no flags) is
        // still a decorate application: the member literal is the last argument.
        let facts = decorate_calls("H([Mobx.observable], C.prototype, \"animation\");\n");
        assert_eq!(
            facts,
            vec![DecorateCallFact {
                callee: "H".to_string(),
                class_anchor: "C".to_string(),
                member: Some("animation".to_string()),
            }],
        );
    }

    #[test]
    fn decorate_call_uses_records_class_decorator_shape() {
        // The class-decorator form `H([d], C)` carries no member literal.
        let facts = decorate_calls("H([ClassDecorator], C);\n");
        assert_eq!(
            facts,
            vec![DecorateCallFact {
                callee: "H".to_string(),
                class_anchor: "C".to_string(),
                member: None,
            }],
        );
    }

    #[test]
    fn decorate_call_uses_requires_array_first_argument() {
        // Without the decorator-array first argument, a 2/3-argument call is an
        // arbitrary call, not a decorate application — no row. The structural array
        // guard is what keeps `passed_to_call`-shaped registrations
        // (`h(x, C.prototype, "m")`) from being misread as decorate applications.
        let facts = decorate_calls("h(x, C.prototype, \"m\");\nObject.assign(C, D);\n");
        assert!(facts.is_empty());
    }

    #[test]
    fn decorate_call_uses_requires_prototype_member_or_bare_class() {
        // A 2nd argument that is a member other than `.prototype`, or a computed
        // access, is not the decorate class anchor — fail-closed, no row.
        let facts = decorate_calls(
            "H([d], C.notPrototype, \"m\");\nH([d], C[k], \"m\");\nH([d], obj.deep.prototype, \"m\");\n",
        );
        assert!(facts.is_empty());
    }

    #[test]
    fn decorate_call_uses_skips_non_string_member_and_overlong_calls() {
        // A non-string 3rd argument (the member slot) is not the decorate shape,
        // and a 5th-or-later positional argument means it is some other call.
        let facts = decorate_calls(
            "H([d], C.prototype, notAString);\nH([d], C.prototype, \"m\", 2, extra);\n",
        );
        assert!(facts.is_empty());
    }

    #[test]
    fn decorate_call_uses_tolerates_unmodeled_statement() {
        // A statement with an unmodeled construct yields no rows but does not abort
        // the scan — the sibling decorate application still contributes.
        let facts = decorate_calls("class C { #x = 1; }\nH([d], C.prototype, \"kept\", 2);\n");
        assert_eq!(
            facts,
            vec![DecorateCallFact {
                callee: "H".to_string(),
                class_anchor: "C".to_string(),
                member: Some("kept".to_string()),
            }],
        );
    }

    fn intrinsic_aliases(src: &str) -> Vec<IntrinsicAliasFact> {
        js_ast::with_swc_globals(|| {
            intrinsic_alias_uses(&js_ast::parse_js_module_ast("<test>", src).unwrap())
        })
    }

    #[test]
    fn intrinsic_alias_uses_records_object_intrinsic_aliases() {
        // The esbuild decorate-trio companions: `var X = Object.defineProperty` /
        // `var X = Object.getOwnPropertyDescriptor`. Each row's target is the alias
        // binding; the property is the re-minify-invariant intrinsic method name.
        let facts = intrinsic_aliases(
            "var p = Object.defineProperty;\nvar g = Object.getOwnPropertyDescriptor;\n",
        );
        assert_eq!(
            facts,
            vec![
                IntrinsicAliasFact {
                    binding: "p".to_string(),
                    property: "defineProperty".to_string(),
                },
                IntrinsicAliasFact {
                    binding: "g".to_string(),
                    property: "getOwnPropertyDescriptor".to_string(),
                },
            ],
        );
    }

    #[test]
    fn intrinsic_alias_uses_accepts_let_and_const_keywords() {
        // The structural shape — not the declaration keyword — is the invariant.
        let facts = intrinsic_aliases(
            "let p = Object.defineProperty;\nconst g = Object.getOwnPropertyDescriptor;\n",
        );
        assert_eq!(
            facts,
            vec![
                IntrinsicAliasFact {
                    binding: "p".to_string(),
                    property: "defineProperty".to_string(),
                },
                IntrinsicAliasFact {
                    binding: "g".to_string(),
                    property: "getOwnPropertyDescriptor".to_string(),
                },
            ],
        );
    }

    #[test]
    fn intrinsic_alias_uses_keeps_aliases_from_decorator_helper_declarator_run() {
        // Real bundles commonly emit the two Object intrinsic aliases and the
        // decorator helper in one multi-declarator `var` statement. The helper body
        // is irrelevant to alias extraction; unsupported helper internals must not
        // make us drop the sibling alias declarators.
        let facts = intrinsic_aliases(
            r#"var define = Object.defineProperty,
  descriptor = Object.getOwnPropertyDescriptor,
  decorate = (decorators, target, key, kind) => {
    for (var desc = descriptor(target, key), index = decorators.length - 1, fn; index >= 0; index--)
      (fn = decorators[index]) && (desc = fn(target, key, desc) || desc);
    return (desc && define(target, key, desc), desc);
  };"#,
        );
        assert_eq!(
            facts,
            vec![
                IntrinsicAliasFact {
                    binding: "define".to_string(),
                    property: "defineProperty".to_string(),
                },
                IntrinsicAliasFact {
                    binding: "descriptor".to_string(),
                    property: "getOwnPropertyDescriptor".to_string(),
                },
            ],
        );
    }

    #[test]
    fn intrinsic_alias_uses_requires_object_base() {
        // An alias off some other identifier (a local, or another global) is not an
        // `Object` intrinsic alias — fail-closed, no row.
        let facts = intrinsic_aliases(
            "var a = Reflect.defineProperty;\nvar b = notObject.defineProperty;\n",
        );
        assert!(facts.is_empty());
    }

    #[test]
    fn intrinsic_alias_uses_requires_non_computed_property() {
        // A computed access `Object[k]` carries no static property name — no row.
        let facts = intrinsic_aliases("var p = Object[\"defineProperty\"];\nvar q = Object[k];\n");
        assert!(facts.is_empty());
    }

    #[test]
    fn intrinsic_alias_uses_fails_closed_on_shadowed_object_declaration() {
        // A chunk-top-level `var Object = ...` shadows the global intrinsic, so the
        // alias might read off the local `Object` — the whole chunk yields no rows.
        let facts = intrinsic_aliases("var Object = {};\nvar p = Object.defineProperty;\n");
        assert!(facts.is_empty());
    }

    #[test]
    fn intrinsic_alias_uses_fails_closed_on_class_named_object() {
        let facts = intrinsic_aliases("class Object {}\nvar p = Object.defineProperty;\n");
        assert!(facts.is_empty());
    }

    #[test]
    fn intrinsic_alias_uses_fails_closed_on_imported_object() {
        // An imported `Object` is a module-local binding, not the global intrinsic.
        let facts = intrinsic_aliases(
            "import { Object } from \"./shim\";\nvar p = Object.defineProperty;\n",
        );
        assert!(facts.is_empty());
    }

    #[test]
    fn intrinsic_alias_uses_fails_closed_on_reassigned_object() {
        // A bare `Object = ...` reassignment defeats the intrinsic-identity guard.
        let facts = intrinsic_aliases("Object = shim;\nvar p = Object.defineProperty;\n");
        assert!(facts.is_empty());
    }

    #[test]
    fn intrinsic_alias_uses_tolerates_unmodeled_statement() {
        // A statement with an unmodeled construct yields no rows but does not abort
        // the scan — the sibling intrinsic alias still contributes. (The unsupported
        // statement does not bind `Object`, so the chunk-level guard stays open.)
        let facts = intrinsic_aliases("class C { #x = 1; }\nvar p = Object.defineProperty;\n");
        assert_eq!(
            facts,
            vec![IntrinsicAliasFact {
                binding: "p".to_string(),
                property: "defineProperty".to_string(),
            }],
        );
    }

    #[test]
    fn node_kind_serde_spelling_matches_tag() {
        // The migration's load-bearing invariant: a `NodeKind`'s serde form is
        // byte-identical to the `as_tag` spelling (the old `&'static str` tag), so
        // `rename_all = "PascalCase"` is a faithful no-op for these already-PascalCase
        // multi-word/multi-capital variants and any serialized form stays stable.
        for kind in [
            NodeKind::VarDecl,
            NodeKind::ExportDefaultDecl,
            NodeKind::MetaPropImportMeta,
            NodeKind::MetaPropNewTarget,
            NodeKind::AsyncGeneratorFunction,
            NodeKind::UpdatePostfix,
            NodeKind::ClassMemberEmpty,
            NodeKind::BindingIdent,
            NodeKind::OptCall,
            NodeKind::TplQuasi,
        ] {
            let json = serde_json::to_string(&kind).unwrap();
            assert_eq!(json, format!("\"{}\"", kind.as_tag()));
            assert_eq!(serde_json::from_str::<NodeKind>(&json).unwrap(), kind);
        }
    }
}
