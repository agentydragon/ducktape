//! The AST fact extractor: a fail-closed walk over a parsed chunk.

use super::{ChunkFacts, NodeId, NodeKind, Unsupported, expr_variant_name, unsupported};
use swc_ecma_ast::{
    ArrayPat, ArrowFunctionBody, AssignTarget, AssignTargetPat, BlockStmt, Callee, Class,
    ClassMember, Decl, DefaultDecl, Expr, ExprOrSpread, ForHead, Function, FunctionBody, Lit,
    MemberExpr, MemberProp, MetaPropKind, MethodKind, ModuleDecl, ModuleItem, ObjectPat,
    ObjectPatProp, OptChainBase, ParamOrTsParamProp, Pat, PrivateName, Prop, PropName,
    PropOrSpread, SimpleAssignTarget, Stmt, SuperProp, Tpl, UsingDecl, VarDecl, VarDeclOrExpr,
    VarDeclarator,
};

#[derive(Default)]
pub(super) struct Extractor {
    pub(super) facts: ChunkFacts,
    next: NodeId,
}

impl Extractor {
    pub(super) fn node(&mut self, kind: NodeKind) -> NodeId {
        let id = self.next;
        self.next += 1;
        self.facts.node_kind.push((id, kind));
        id
    }

    pub(super) fn module_item(
        &mut self,
        item: &ModuleItem,
        ordinal: usize,
    ) -> Result<NodeId, Unsupported> {
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
                    let local = spec.local();
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
        self.var_declarators(var, &var.decls)
    }

    pub(super) fn var_declarators(
        &mut self,
        var: &VarDecl,
        declarators: &[VarDeclarator],
    ) -> Result<NodeId, Unsupported> {
        let id = self.node(NodeKind::VarDecl);
        // The declaration keyword distinguishes `let`/`const`/`var` — a label
        // the matcher compares, so a `let` selector cannot match a `const`.
        self.facts
            .operator
            .push((id, js_ast::var_decl_kind_str(var.kind).to_string()));
        for (index, declarator) in declarators.iter().enumerate() {
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
        if !function.decorators.is_empty()
            || function.params.iter().any(|p| !p.decorators.is_empty())
        {
            return unsupported("function decorators");
        }
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
            let block = self.function_body(body)?;
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

    fn function_body(&mut self, body: &FunctionBody) -> Result<NodeId, Unsupported> {
        let id = self.node(NodeKind::Block);
        for (index, stmt) in body.stmts.iter().enumerate() {
            let stmt = self.stmt(stmt)?;
            self.facts.child.push((id, index as u32, stmt));
        }
        Ok(id)
    }

    fn class_node(&mut self, class: &Class) -> Result<NodeId, Unsupported> {
        if !class.decorators.is_empty() {
            return unsupported("class decorators");
        }
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

    fn class_method_node(&mut self, is_static: bool, kind: MethodKind) -> NodeId {
        let modifier = match (is_static, kind) {
            (false, MethodKind::Method) => "method",
            (false, MethodKind::Getter) => "get",
            (false, MethodKind::Setter) => "set",
            (true, MethodKind::Method) => "static method",
            (true, MethodKind::Getter) => "static get",
            (true, MethodKind::Setter) => "static set",
        };
        let id = self.node(NodeKind::Method);
        self.facts.operator.push((id, modifier.into()));
        id
    }

    fn class_property_node(&mut self, is_static: bool) -> NodeId {
        let id = self.node(NodeKind::ClassProp);
        self.facts
            .operator
            .push((id, if is_static { "static" } else { "instance" }.into()));
        id
    }

    fn class_member(&mut self, member: &ClassMember) -> Result<NodeId, Unsupported> {
        match member {
            ClassMember::Method(method) => {
                let id = self.class_method_node(method.is_static, method.kind);
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
                            if !param.decorators.is_empty() {
                                return unsupported("constructor parameter decorators");
                            }
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
                    let body = self.function_body(body)?;
                    self.facts.child.push((id, next, body));
                }
                Ok(id)
            }
            ClassMember::ClassProp(prop) => {
                if !prop.decorators.is_empty() {
                    return unsupported("class property decorators");
                }
                let id = self.class_property_node(prop.is_static);
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
                let id = self.class_method_node(method.is_static, method.kind);
                let key = self.private_name_key(&method.key);
                self.facts.child.push((id, 0, key));
                let function = self.function(&method.function)?;
                self.facts.child.push((id, 1, function));
                Ok(id)
            }
            ClassMember::PrivateProp(prop) => {
                if !prop.decorators.is_empty() {
                    return unsupported("class property decorators");
                }
                let id = self.class_property_node(prop.is_static);
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
    /// `#`-prefixed, matching the label convention of
    /// `selectors/authoring/{readoff_render,selector_candidate_index,shape_index}.rs`,
    /// so a private key can never collide with a same-named public one.
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
                    ArrowFunctionBody::FunctionBody(block) => self.function_body(block)?,
                    ArrowFunctionBody::Expr(expr) => self.expr(expr)?,
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
                    if let Some(body) = &getter.function.body {
                        let body = self.function_body(body)?;
                        self.facts.child.push((id, 1, body));
                    }
                    Ok(id)
                }
                Prop::Setter(setter) => {
                    let id = self.node(NodeKind::Setter);
                    let key = self.prop_key(&setter.key)?;
                    self.facts.child.push((id, 0, key));
                    let param = self.pat(&setter.function.params[0].pat)?;
                    self.facts.child.push((id, 1, param));
                    if let Some(body) = &setter.function.body {
                        let body = self.function_body(body)?;
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
