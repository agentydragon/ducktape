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
//! Coverage grows construct by construct until the corpus extracts with zero
//! `Unsupported`. This is the seed slice — it faithfully covers simple
//! declarator / call / member / literal shapes and errors on everything else;
//! it is intentionally not yet complete.

use std::collections::BTreeMap;

use js_ast::statement_ordinal_for_body_index;
use swc_ecma_ast::{
    BlockStmt, Callee, Class, ClassMember, Decl, Expr, ExprOrSpread, Function, Lit, MemberExpr,
    MemberProp, Module, ModuleDecl, ModuleItem, Pat, Prop, PropName, PropOrSpread, Stmt,
    VarDeclarator,
};

pub type NodeId = u32;

/// A faithful relational projection of one chunk's top-level statements. Each
/// vector is an EDB relation the lowered selector queries join over.
#[derive(Debug, Default)]
pub struct ChunkFacts {
    /// node -> syntactic kind tag.
    pub node_kind: Vec<(NodeId, &'static str)>,
    /// parent -> (source-order ordinal, child). The ordinal is what the
    /// run-hole / adjacency encoding compares (`i < j`).
    pub child: Vec<(NodeId, u32, NodeId)>,
    /// string-literal value, unescaped via `js_ast::str_value`.
    pub str_lit: Vec<(NodeId, String)>,
    /// numeric-literal token, rendered faithfully (source `raw` when present).
    pub num_lit: Vec<(NodeId, String)>,
    pub bool_lit: Vec<(NodeId, bool)>,
    /// identifier spelling (the `identifiers: exact` surface).
    pub ident_name: Vec<(NodeId, String)>,
    /// member / property / method name (non-computed).
    pub prop_name: Vec<(NodeId, String)>,
    /// operator token for `Bin` / `Unary` nodes (a T-invariant label).
    pub operator: Vec<(NodeId, String)>,
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
    fn node(&mut self, kind: &'static str) -> NodeId {
        let id = self.next;
        self.next += 1;
        self.facts.node_kind.push((id, kind));
        id
    }

    fn module_item(&mut self, item: &ModuleItem, ordinal: usize) -> Result<NodeId, Unsupported> {
        let id = match item {
            ModuleItem::Stmt(stmt) => self.stmt(stmt)?,
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
                let id = self.node("ExportDecl");
                let inner = self.decl(&export.decl)?;
                self.facts.child.push((id, 0, inner));
                id
            }
            ModuleItem::ModuleDecl(_) => return unsupported("module_item: module_decl"),
        };
        self.facts.top_level.push((id, ordinal));
        Ok(id)
    }

    fn stmt(&mut self, stmt: &Stmt) -> Result<NodeId, Unsupported> {
        match stmt {
            Stmt::Decl(decl) => self.decl(decl),
            Stmt::Expr(expr_stmt) => {
                let id = self.node("ExprStmt");
                let inner = self.expr(&expr_stmt.expr)?;
                self.facts.child.push((id, 0, inner));
                Ok(id)
            }
            Stmt::Return(ret) => {
                let id = self.node("Return");
                if let Some(arg) = &ret.arg {
                    let arg = self.expr(arg)?;
                    self.facts.child.push((id, 0, arg));
                }
                Ok(id)
            }
            _ => unsupported("stmt"),
        }
    }

    fn decl(&mut self, decl: &Decl) -> Result<NodeId, Unsupported> {
        match decl {
            Decl::Var(var) => {
                let id = self.node("VarDecl");
                for (index, declarator) in var.decls.iter().enumerate() {
                    let d = self.var_declarator(declarator)?;
                    self.facts.child.push((id, index as u32, d));
                }
                Ok(id)
            }
            Decl::Fn(fn_decl) => {
                let id = self.node("FnDecl");
                let name = self.node("Ident");
                self.facts
                    .ident_name
                    .push((name, fn_decl.ident.sym.to_string()));
                self.facts.child.push((id, 0, name));
                let function = self.function(&fn_decl.function)?;
                self.facts.child.push((id, 1, function));
                Ok(id)
            }
            Decl::Class(class_decl) => {
                let id = self.node("ClassDecl");
                let name = self.node("Ident");
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

    fn var_declarator(&mut self, declarator: &VarDeclarator) -> Result<NodeId, Unsupported> {
        let id = self.node("VarDeclarator");
        let name = self.pat(&declarator.name)?;
        self.facts.child.push((id, 0, name));
        if let Some(init) = &declarator.init {
            let init = self.expr(init)?;
            self.facts.child.push((id, 1, init));
        }
        Ok(id)
    }

    fn function(&mut self, function: &Function) -> Result<NodeId, Unsupported> {
        let id = self.node("Function");
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
        let id = self.node("Block");
        for (index, stmt) in block.stmts.iter().enumerate() {
            let stmt = self.stmt(stmt)?;
            self.facts.child.push((id, index as u32, stmt));
        }
        Ok(id)
    }

    fn class_node(&mut self, class: &Class) -> Result<NodeId, Unsupported> {
        let id = self.node("Class");
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
                let id = self.node("Method");
                let key = self.prop_key(&method.key)?;
                self.facts.child.push((id, 0, key));
                let function = self.function(&method.function)?;
                self.facts.child.push((id, 1, function));
                Ok(id)
            }
            _ => unsupported("class_member"),
        }
    }

    fn prop_key(&mut self, key: &PropName) -> Result<NodeId, Unsupported> {
        match key {
            PropName::Ident(name) => {
                let id = self.node("PropName");
                self.facts.prop_name.push((id, name.sym.to_string()));
                Ok(id)
            }
            PropName::Str(value) => {
                let id = self.node("PropName");
                self.facts.prop_name.push((id, js_ast::str_value(value)));
                Ok(id)
            }
            _ => unsupported("prop_key"),
        }
    }

    fn pat(&mut self, pat: &Pat) -> Result<NodeId, Unsupported> {
        match pat {
            Pat::Ident(binding) => {
                let id = self.node("BindingIdent");
                self.facts.ident_name.push((id, binding.id.sym.to_string()));
                Ok(id)
            }
            _ => unsupported("pat"),
        }
    }

    fn expr(&mut self, expr: &Expr) -> Result<NodeId, Unsupported> {
        match expr {
            Expr::Ident(ident) => {
                let id = self.node("Ident");
                self.facts.ident_name.push((id, ident.sym.to_string()));
                Ok(id)
            }
            Expr::Lit(lit) => self.lit(lit),
            Expr::Member(member) => self.member(member),
            Expr::Call(call) => {
                let id = self.node("Call");
                match &call.callee {
                    Callee::Expr(callee) => {
                        let callee = self.expr(callee)?;
                        self.facts.child.push((id, 0, callee));
                    }
                    _ => return unsupported("callee"),
                }
                self.push_args(id, 1, &call.args)?;
                Ok(id)
            }
            Expr::New(new) => {
                let id = self.node("New");
                let callee = self.expr(&new.callee)?;
                self.facts.child.push((id, 0, callee));
                if let Some(args) = &new.args {
                    self.push_args(id, 1, args)?;
                }
                Ok(id)
            }
            Expr::Bin(bin) => {
                let id = self.node("Bin");
                self.facts.operator.push((id, bin.op.as_str().to_string()));
                let left = self.expr(&bin.left)?;
                self.facts.child.push((id, 0, left));
                let right = self.expr(&bin.right)?;
                self.facts.child.push((id, 1, right));
                Ok(id)
            }
            Expr::Unary(unary) => {
                let id = self.node("Unary");
                self.facts
                    .operator
                    .push((id, unary.op.as_str().to_string()));
                let arg = self.expr(&unary.arg)?;
                self.facts.child.push((id, 0, arg));
                Ok(id)
            }
            Expr::Cond(cond) => {
                let id = self.node("Cond");
                let test = self.expr(&cond.test)?;
                self.facts.child.push((id, 0, test));
                let cons = self.expr(&cond.cons)?;
                self.facts.child.push((id, 1, cons));
                let alt = self.expr(&cond.alt)?;
                self.facts.child.push((id, 2, alt));
                Ok(id)
            }
            Expr::Seq(seq) => {
                let id = self.node("Seq");
                for (index, item) in seq.exprs.iter().enumerate() {
                    let item = self.expr(item)?;
                    self.facts.child.push((id, index as u32, item));
                }
                Ok(id)
            }
            Expr::Array(array) => {
                let id = self.node("Array");
                for (index, elem) in array.elems.iter().enumerate() {
                    match elem {
                        Some(elem) => {
                            if elem.spread.is_some() {
                                return unsupported("array: spread");
                            }
                            let value = self.expr(&elem.expr)?;
                            self.facts.child.push((id, index as u32, value));
                        }
                        // Elision keeps positions faithful (`[a, , b]`).
                        None => {
                            let elision = self.node("Elision");
                            self.facts.child.push((id, index as u32, elision));
                        }
                    }
                }
                Ok(id)
            }
            Expr::Fn(fn_expr) => {
                let id = self.node("FnExpr");
                if let Some(ident) = &fn_expr.ident {
                    let name = self.node("Ident");
                    self.facts.ident_name.push((name, ident.sym.to_string()));
                    self.facts.child.push((id, 0, name));
                }
                let function = self.function(&fn_expr.function)?;
                self.facts.child.push((id, 1, function));
                Ok(id)
            }
            Expr::Object(object) => {
                let id = self.node("Object");
                for (index, prop) in object.props.iter().enumerate() {
                    let prop = self.object_prop(prop)?;
                    self.facts.child.push((id, index as u32, prop));
                }
                Ok(id)
            }
            // Parentheses are transparent: the matcher matches modulo grouping.
            Expr::Paren(paren) => self.expr(&paren.expr),
            other => unsupported(expr_variant_name(other)),
        }
    }

    fn object_prop(&mut self, prop: &PropOrSpread) -> Result<NodeId, Unsupported> {
        match prop {
            PropOrSpread::Spread(spread) => {
                let id = self.node("Spread");
                let expr = self.expr(&spread.expr)?;
                self.facts.child.push((id, 0, expr));
                Ok(id)
            }
            PropOrSpread::Prop(prop) => match &**prop {
                Prop::Shorthand(ident) => {
                    let id = self.node("Shorthand");
                    self.facts.ident_name.push((id, ident.sym.to_string()));
                    Ok(id)
                }
                Prop::KeyValue(key_value) => {
                    let id = self.node("KeyValue");
                    let key = self.prop_key(&key_value.key)?;
                    self.facts.child.push((id, 0, key));
                    let value = self.expr(&key_value.value)?;
                    self.facts.child.push((id, 1, value));
                    Ok(id)
                }
                Prop::Method(method) => {
                    let id = self.node("ObjectMethod");
                    let key = self.prop_key(&method.key)?;
                    self.facts.child.push((id, 0, key));
                    let function = self.function(&method.function)?;
                    self.facts.child.push((id, 1, function));
                    Ok(id)
                }
                _ => unsupported("object: prop"),
            },
        }
    }

    fn push_args(
        &mut self,
        parent: NodeId,
        base: u32,
        args: &[ExprOrSpread],
    ) -> Result<(), Unsupported> {
        for (index, arg) in args.iter().enumerate() {
            if arg.spread.is_some() {
                return unsupported("call/new: spread arg");
            }
            let arg = self.expr(&arg.expr)?;
            self.facts.child.push((parent, base + index as u32, arg));
        }
        Ok(())
    }

    fn member(&mut self, member: &MemberExpr) -> Result<NodeId, Unsupported> {
        let id = self.node("Member");
        let obj = self.expr(&member.obj)?;
        self.facts.child.push((id, 0, obj));
        match &member.prop {
            MemberProp::Ident(name) => {
                let prop = self.node("PropName");
                self.facts.prop_name.push((prop, name.sym.to_string()));
                self.facts.child.push((id, 1, prop));
            }
            MemberProp::Computed(computed) => {
                let computed = self.expr(&computed.expr)?;
                self.facts.child.push((id, 1, computed));
            }
            MemberProp::PrivateName(_) => return unsupported("member: private name"),
        }
        Ok(id)
    }

    fn lit(&mut self, lit: &Lit) -> Result<NodeId, Unsupported> {
        match lit {
            Lit::Str(value) => {
                let id = self.node("StrLit");
                self.facts.str_lit.push((id, js_ast::str_value(value)));
                Ok(id)
            }
            Lit::Num(number) => {
                let id = self.node("NumLit");
                let token = number
                    .raw
                    .as_ref()
                    .map(|raw| raw.to_string())
                    .unwrap_or_else(|| number.value.to_string());
                self.facts.num_lit.push((id, token));
                Ok(id)
            }
            Lit::Bool(boolean) => {
                let id = self.node("BoolLit");
                self.facts.bool_lit.push((id, boolean.value));
                Ok(id)
            }
            _ => unsupported("lit"),
        }
    }
}

/// Project a parsed chunk's top-level statements into AST facts, or fail loudly
/// at the first construct not yet modeled.
pub fn extract_facts(module: &Module) -> Result<ChunkFacts, Unsupported> {
    let mut extractor = Extractor::default();
    for (body_idx, item) in module.body.iter().enumerate() {
        let ordinal = statement_ordinal_for_body_index(&module.body, body_idx);
        extractor.module_item(item, ordinal)?;
    }
    Ok(extractor.facts)
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
    use std::collections::BTreeSet;

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

        let kinds: BTreeSet<&str> = facts.node_kind.iter().map(|(_, k)| *k).collect();
        for expected in [
            "VarDecl",
            "VarDeclarator",
            "BindingIdent",
            "Call",
            "Member",
            "PropName",
            "Ident",
            "StrLit",
        ] {
            assert!(
                kinds.contains(expected),
                "kind {expected} present: {kinds:?}"
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
        let kinds: BTreeSet<&str> = facts.node_kind.iter().map(|(_, k)| *k).collect();
        for expected in [
            "FnDecl",
            "Function",
            "Block",
            "Return",
            "Call",
            "Ident",
            "BindingIdent",
        ] {
            assert!(
                kinds.contains(expected),
                "kind {expected} present: {kinds:?}"
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

        let kinds: BTreeSet<&str> = facts.node_kind.iter().map(|(_, k)| *k).collect();
        for expected in [
            "ClassDecl",
            "Class",
            "Method",
            "PropName",
            "Function",
            "Block",
            "Return",
            "StrLit",
        ] {
            assert!(
                kinds.contains(expected),
                "kind {expected} present: {kinds:?}"
            );
        }
    }

    #[test]
    fn extracts_common_expression_variants() {
        // (b + c) ? new D(e) : [f] — binary, conditional, new, array.
        let facts = extract("const a = b + c ? new D(e) : [f];").expect("covered shape extracts");

        let kinds: BTreeSet<&str> = facts.node_kind.iter().map(|(_, k)| *k).collect();
        for expected in ["Cond", "Bin", "New", "Array"] {
            assert!(
                kinds.contains(expected),
                "kind {expected} present: {kinds:?}"
            );
        }
        let operators: Vec<&str> = facts.operator.iter().map(|(_, s)| s.as_str()).collect();
        assert_eq!(operators, vec!["+"], "binary operator token");
        let idents: BTreeSet<&str> = facts.ident_name.iter().map(|(_, s)| s.as_str()).collect();
        for name in ["a", "b", "c", "D", "e", "f"] {
            assert!(idents.contains(name), "ident {name} present: {idents:?}");
        }
    }

    #[test]
    fn extracts_function_expression_and_object_literal() {
        let facts = extract("const a = { handler: function (x) { return x; }, ...rest };")
            .expect("covered shape extracts");
        let kinds: BTreeSet<&str> = facts.node_kind.iter().map(|(_, k)| *k).collect();
        for expected in ["Object", "KeyValue", "FnExpr", "Function", "Spread"] {
            assert!(
                kinds.contains(expected),
                "kind {expected} present: {kinds:?}"
            );
        }
        let props: Vec<&str> = facts.prop_name.iter().map(|(_, s)| s.as_str()).collect();
        assert_eq!(props, vec!["handler"], "object key");
    }

    #[test]
    fn coverage_report_tallies_per_statement() {
        // One extractable statement; one blocked by an unmodeled `debugger`
        // statement. One statement's gap does not abort the tally of the other.
        let report = js_ast::with_swc_globals(|| {
            coverage_report(
                &js_ast::parse_js_module_ast("<test>", "const a = \"s\";\ndebugger;\n").unwrap(),
            )
        });
        assert_eq!(report.total, 2);
        assert_eq!(report.covered, 1);
        assert_eq!(report.unsupported.get("stmt"), Some(&1));
    }

    #[test]
    fn unmodeled_construct_errors_loudly_not_silently() {
        // A `debugger` statement is not selector-relevant and stays unmodeled.
        // Fail-closed means a hard error here, never a silently-incomplete fact
        // set that would let a query under-constrain.
        let error = extract("debugger;").unwrap_err();
        assert_eq!(error.context, "stmt");
    }
}
